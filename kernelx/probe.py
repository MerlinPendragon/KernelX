"""Read-only, bounded evidence collection. Never initializes torch/NPU runtimes."""
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time
import uuid
from urllib.parse import unquote, urlparse
from datetime import datetime, timezone
from pathlib import Path

from .protocol import digest, validate

PARSER_VERSION = "ascend-probe-v3"
# Explicit product allowlist from Ascend's pinned SOC_TO_SHORT_SOC_MAP.
# Other names remain unknown; identifying a model does not verify runtime support.
BIN_MAPPING_SOURCE = "https://gitee.com/ascend/samples/blob/84504315a553ab84e2ccaec6bf95a75a9e68ad66/operator_contrib/CumsumSample/FrameworkLaunch/Cumsum/cmake/util/opdesc_parser.py"
BIN_MAPPING = {alias: ("Ascend 910B", "Ascend " + model)
               for model in ("910B1", "910B2", "910B2C", "910B3", "910B4")
               for alias in (model, "Ascend " + model, "Ascend" + model)}
BIN_MAPPING_VERSION = "ascend-910b-products-v2"
LIBRARIES = ("cann-opp", "ops-nn", "ops-transformer", "sgl-kernel-npu", "tile-kernels", "deepgemm-ascend", "deepep-ascend")
PACKAGES = ("torch", "torch-npu", "ops-nn", "sgl-kernel-npu", "tile-kernels", "deepgemm-ascend", "deepep-ascend", "ops-transformer")
PACKAGE_ALIASES = {'deepgemm-ascend':'deep_gemm','deepep-ascend':'deep_ep'}
PACKAGES += ('deep_gemm','deep_ep','triton','tilelang','deep-jit')


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def classify(returncode, output):
    low = output.lower()
    if "permission denied" in low or "insufficient permission" in low or "operation not permitted" in low:
        return "PERMISSION_DENIED"
    if "unrecognized option" in low or "error parameter" in low or "not support" in low:
        return "UNSUPPORTED"
    if "no such file or directory" in low and "error while loading shared libraries" not in low:
        return "NOT_FOUND"
    if returncode != 0:
        return "COMMAND_FAILED"
    return "KNOWN"


def fields(output):
    result = {}
    for line in output.splitlines():
        match = re.match(r"\s*([^:]+?)\s*:\s*(.*?)\s*$", line)
        if match:
            result[match[1].strip()] = match[2].strip()
        else:
            match = re.match(r"\s*([^=]+?)\s*=\s*(.*?)\s*$", line)
            if match:
                result[match[1].strip()] = match[2].strip()
    return result


def fact(value=None, status="UNKNOWN", reason="not observed", source=(), confidence="NONE"):
    if value is not None:
        value = str(value)
    return dict(value=value, status=status, reason=reason, source=list(source), confidence=confidence)


def from_record(record, key, confidence="VERIFIED"):
    if record["execution_status"] != "KNOWN":
        return fact(status=record["execution_status"], reason=record["reason"], source=[record["evidence_id"]])
    value = fields(record["stdout"]).get(key)
    if not value or value.upper() in ("NA", "N/A", "UNKNOWN"):
        return fact(reason="field absent or unavailable: " + key, source=[record["evidence_id"]])
    record["parse_status"] = "KNOWN"
    return fact(value, "KNOWN", None, [record["evidence_id"]], confidence)


def parse_mapping(record):
    if record["execution_status"] != "KNOWN":
        return []
    rows = []
    columns = None
    for line in record["stdout"].splitlines():
        if line.strip().startswith("NPU ID"):
            columns = re.split(r"\s{2,}", line.strip())
            continue
        if columns == ["NPU ID", "Chip ID", "Chip Logic ID", "Chip Name"]:
            match = re.fullmatch(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(.+?)\s*", line)
            if match:
                rows.append(dict(npu_id=int(match[1]), chip_id=int(match[2]), logical_id=int(match[3]), chip_name_raw=match[4]))
        elif columns == ["NPU ID", "Slot ID", "Chip ID", "Chip Phy-ID", "Chip Name"]:
            match = re.fullmatch(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(.+?)\s*", line)
            if not match:
                continue
            # This table has no logical-ID column. Support only the bare-host
            # direct layout observed on 950DT, never a general Phy->Logic mapping.
            npu, slot, chip, physical = map(int, match.group(1, 2, 3, 4))
            if chip != 0 or npu != physical or any(name in os.environ for name in
                    ("ASCEND_RT_VISIBLE_DEVICES", "ASCEND_VISIBLE_DEVICES")):
                record["parse_status"] = "PARSE_ERROR"
                record["reason"] = "Physical-ID table requires chip 0, NPU ID == Phy-ID and no device visibility remapping"
                return []
            rows.append(dict(npu_id=npu, chip_id=chip, logical_id=physical, chip_name_raw=match[5]))
    record["parse_status"] = "KNOWN" if rows else "PARSE_ERROR"
    if not rows:
        record["reason"] = "No accelerator rows in a recognized npu-smi mapping header; MCU rows are excluded"
    return rows


class Collector:
    def __init__(self, server_id, redact=True, timeout=15):
        self.server_id = str(uuid.UUID(server_id))
        self.redact = redact
        self.timeout = timeout
        self.evidence = []

    def identity_token(self, value):
        """Same pseudonym in both display modes; also accepts redacted replay."""
        if not value:
            return None
        value = value.strip()
        if re.fullmatch(r"redacted:[0-9a-f]{64}", value):
            return value
        if value.upper() in ("NA", "N/A", "UNKNOWN") or not re.search(r"[1-9A-Fa-f]", value):
            return None
        return "redacted:" + hashlib.sha256((self.server_id + value).encode()).hexdigest()

    def sanitize(self, text):
        if not self.redact:
            return text
        # Host-local identifiers are pseudonymized before serialization or hashing.
        def redact_identity(match):
            value = match[2].strip()
            return match[1] + (self.identity_token(value) or "NA")
        text = re.sub(r"(?im)^(\s*(?:VDie ID|NDie ID|Die ID|Serial Number|Serial No\.?|SN)\s*:\s*)(.+)$", redact_identity, text)
        hostname = socket.gethostname()
        if hostname:
            text = text.replace(hostname, "<hostname>")
        home = str(Path.home())
        if home != "/":
            text = text.replace(home, "/home/<user>")
        text = re.sub(r"/home/[^/\s]+", "/home/<user>", text)
        # Dotted versions are not addresses. Protect only explicit version fields
        # and tool version headers, including JSON package metadata.
        version_context = re.compile(
            r'(?i)(?:"[\w-]*version(?:_fw)?"\s*:\s*"[^"\n]*"'
            r'|(?<![\w])(?:[\w-]*version(?:_fw)?)\s*[:=]\s*(?:"[^"\n]*"|[^\s;,"\n]+)'
            r'|\b(?:npu-smi|msprof|version)\s+\d[\w.\-]*)')
        protected = [(m.start(), m.end()) for m in version_context.finditer(text)]
        text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
                      lambda m: m[0] if any(start <= m.start() < end for start, end in protected) else "<ip>", text)
        return text

    def record(self, argv, stdout="", stderr="", exit_code=0, status=None, duration=0, started=None, reason=None):
        stdout, stderr = self.sanitize(stdout), self.sanitize(stderr)
        status = status or classify(exit_code, stdout + stderr)
        entry = dict(evidence_id="e%04d" % len(self.evidence), argv=[self.sanitize(a) for a in argv],
                     started_at=started or now(), duration_seconds=duration, exit_code=exit_code,
                     stdout=stdout, stderr=stderr, execution_status=status,
                     parse_status="UNKNOWN" if status == "KNOWN" else status,
                     reason=reason if reason else ("raw evidence; no parser assigned" if status == "KNOWN" else status.lower()),
                     permission_scope="euid=" + str(os.geteuid()) + "; no privilege elevation",
                     parser_version=PARSER_VERSION, redacted=self.redact,
                     output_sha256=digest({"stdout": stdout, "stderr": stderr}))
        self.evidence.append(entry)
        return entry

    def run(self, argv):
        started, monotonic = now(), time.monotonic()
        try:
            result = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=self.timeout, check=False)
            return self.record(argv, result.stdout, result.stderr, result.returncode,
                               duration=time.monotonic() - monotonic, started=started)
        except FileNotFoundError as exc:
            return self.record(argv, stderr=str(exc), exit_code=None, status="NOT_FOUND", started=started,
                               duration=time.monotonic() - monotonic)
        except PermissionError as exc:
            return self.record(argv, stderr=str(exc), exit_code=None, status="PERMISSION_DENIED", started=started,
                               duration=time.monotonic() - monotonic)
        except subprocess.TimeoutExpired as exc:
            def decode(value): return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")
            return self.record(argv, decode(exc.stdout), decode(exc.stderr), exit_code=None, status="TIMEOUT",
                               duration=time.monotonic() - monotonic, started=started)

    def read(self, path):
        return self.run(["cat", str(path)])


def _package(collector, name):
    try:
        dist = importlib.metadata.distribution(name)
        location = str(Path(dist.locate_file("")).resolve())
        record = collector.record(["importlib.metadata", name], json.dumps({"version": dist.version, "path": location}))
        record["parse_status"] = "KNOWN"
        return fact(dist.version, "KNOWN", None, [record["evidence_id"]], "DECLARED_ONLY"), collector.sanitize(location)
    except importlib.metadata.PackageNotFoundError:
        record = collector.record(["importlib.metadata", name], exit_code=None, status="NOT_FOUND", reason="not installed in probe interpreter")
        return fact(status="NOT_FOUND", reason=record["reason"], source=[record["evidence_id"]]), None



def _library_provenance(collector, name, configured_root=None):
    """Package provenance and explicitly associated source trees, never guessed repos."""
    result = dict(package_id=None, repository_url=None, git_commit=None)
    sources = []
    root = configured_root
    commit_reason = "no Git provenance in installed package or configured source tree"
    try:
        dist = importlib.metadata.distribution(name)
        result["package_id"] = name + "==" + dist.version
        raw = dist.read_text("direct_url.json")
        if raw:
            record = collector.record(["package-direct-url", name], raw)
            sources.append(record["evidence_id"])
            try:
                data = json.loads(raw)
                vcs = data.get("vcs_info", {})
                if vcs.get("vcs") == "git" and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", vcs.get("commit_id", "")):
                    result.update(repository_url=data.get("url"), git_commit=vcs["commit_id"])
                    commit_reason = None
                # Editable local installs explicitly identify their source directory.
                if not root and data.get("dir_info", {}).get("editable") and urlparse(data.get("url", "")).scheme == "file":
                    root = unquote(urlparse(data["url"]).path)
            except (ValueError, TypeError, AttributeError):
                commit_reason = "invalid package direct_url.json"
    except importlib.metadata.PackageNotFoundError:
        pass
    if root:
        root = str(Path(root).resolve())
        record = collector.run(["git", "-C", root, "rev-parse", "--show-toplevel"])
        sources.append(record["evidence_id"])
        # A configured library root must itself be the repository root. This avoids
        # attributing a parent workspace's unrelated commit to an installed library.
        if record["exit_code"] == 0 and record["stdout"].strip() == collector.sanitize(root):
            head = collector.run(["git", "-C", root, "rev-parse", "HEAD"])
            sources.append(head["evidence_id"])
            value = head["stdout"].strip()
            if head["exit_code"] == 0 and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
                result["git_commit"] = value
                commit_reason = None
                origin = collector.run(["git", "-C", root, "config", "--get", "remote.origin.url"])
                sources.append(origin["evidence_id"])
                result["repository_url"] = origin["stdout"].strip() if origin["exit_code"] == 0 else None
                state = collector.run(["git", "-C", root, "status", "--porcelain", "--untracked-files=all"])
                sources.append(state["evidence_id"])
                result["source_tree_dirty"] = bool(state["stdout"].strip()) if state["exit_code"] == 0 else None
            else:
                commit_reason = "configured source tree HEAD unavailable"
        else:
            commit_reason = "source directory is not a confirmed Git repository root"
        result["source_root"] = collector.sanitize(root)
    result["repository_url"] = collector.sanitize(result["repository_url"]) if result["repository_url"] else None
    result.update(commit_source=sources, commit_reason=commit_reason,
                  commit_status="KNOWN" if commit_reason is None else "UNKNOWN",
                  confidence="DECLARED_ONLY" if commit_reason is None else "NONE")
    return result


def probe(server_id, redact=True, timeout=15):
    collector = Collector(server_id, redact, timeout)
    host_record = collector.record(["python", "platform"], json.dumps(dict(hostname=socket.gethostname(), architecture=platform.machine(), kernel=platform.release(), os=platform.platform())))
    host_record["parse_status"] = "KNOWN"
    host = {name: fact(collector.sanitize(value), "KNOWN", None, [host_record["evidence_id"]], "VERIFIED") for name, value in
            dict(hostname=socket.gethostname(), architecture=platform.machine(), kernel=platform.release(), os=platform.platform()).items()}
    numa = collector.run(["lscpu"])
    if numa["execution_status"] == "KNOWN":
        numa["parse_status"] = "KNOWN"
        host["cpu_numa"] = fact(numa["stdout"].strip(), "KNOWN", None, [numa["evidence_id"]], "VERIFIED")
    else:
        host["cpu_numa"] = fact(status=numa["execution_status"], reason=numa["reason"], source=[numa["evidence_id"]])
    smi_info = collector.run(["npu-smi", "info"])
    smi_match = re.search(r"npu-smi\s+([0-9][\w.\-]+)", smi_info["stdout"])
    smi_version = fact(smi_match[1], "KNOWN", None, [smi_info["evidence_id"]], "VERIFIED") if smi_match else fact(status=smi_info["execution_status"] if smi_info["execution_status"] != "KNOWN" else "PARSE_ERROR", reason="npu-smi version header unavailable", source=[smi_info["evidence_id"]])
    if smi_match:
        smi_info["parse_status"] = "KNOWN"
    collector.run(["npu-smi", "info", "-l"])
    mapping = collector.run(["npu-smi", "info", "-m"])
    rows = parse_mapping(mapping)
    devices = []
    for row in rows:
        board = collector.run(["npu-smi", "info", "-t", "board", "-i", str(row["npu_id"]), "-c", str(row["chip_id"])])
        usages = collector.run(["npu-smi", "info", "-t", "usages", "-i", str(row["npu_id"]), "-c", str(row["chip_id"])])
        data = fields(board["stdout"]) if board["execution_status"] == "KNOWN" else {}
        full_name = data.get("Chip Name")
        normalized = BIN_MAPPING.get(full_name)
        mapping_normalized = BIN_MAPPING.get(row["chip_name_raw"])
        if normalized and normalized == mapping_normalized:
            soc = fact(normalized[0], "KNOWN", None, [mapping["evidence_id"], board["evidence_id"]], "VERIFIED")
            bin_value = fact(normalized[1], "KNOWN", None, [mapping["evidence_id"], board["evidence_id"]], "VERIFIED")
        else:
            status = board["execution_status"] if board["execution_status"] != "KNOWN" else "UNKNOWN"
            reason = "board/mapping not both confirmed by " + BIN_MAPPING_VERSION
            soc = fact(status=status, reason=reason, source=[mapping["evidence_id"], board["evidence_id"]])
            bin_value = fact(status=status, reason=reason, source=soc["source"])
        pcie = from_record(board, "PCIe Bus Info")
        die = collector.identity_token(data.get("VDie ID")) or collector.identity_token(data.get("Die ID"))
        # PCIe identifies a location, never a replaceable physical card uniquely.
        location = pcie["value"] or ("npu-%d-chip-%d" % (row["npu_id"], row["chip_id"]))
        chip_token = die or location
        device = dict(row, device_uid=str(uuid.uuid5(uuid.UUID(collector.server_id), "chip:" + chip_token)),
                      card_uid=str(uuid.uuid5(uuid.UUID(collector.server_id), "card-location:" + location)),
                      identity_confidence="STABLE_CHIP" if die else "LOCATION_ONLY",
                      card_identity_confidence="LOCATION_ONLY", soc_family=soc, hardware_bin=bin_value,
                      bin_mapping_version=BIN_MAPPING_VERSION, board_id=from_record(board, "Board ID"), pcie=pcie,
                      hbm_mb=from_record(usages, "HBM Capacity(MB)"), firmware=from_record(board, "Firmware Version"))
        devices.append(device)
    inventory = fact(str(len(devices)), "KNOWN", None, [mapping["evidence_id"]], "VERIFIED") if devices else fact(status=mapping["parse_status"], reason=mapping["reason"] or "no mapped accelerators", source=[mapping["evidence_id"]])
    driver_record = collector.read("/usr/local/Ascend/driver/version.info")
    firmware_record = collector.read("/usr/local/Ascend/firmware/version.info")
    toolkit_home = os.environ.get("ASCEND_HOME_PATH") or os.environ.get("ASCEND_TOOLKIT_HOME") or "/usr/local/Ascend/ascend-toolkit/latest"
    root = Path(toolkit_home).resolve()
    opp = Path(os.environ.get("ASCEND_OPP_PATH", str(root / "opp"))).resolve()
    install = collector.read(root / (platform.machine() + "-linux") / "ascend_toolkit_install.info")
    # Keep the legacy installation file as evidence, never infer version from dirname.
    toolkit = from_record(install, "version", confidence="DECLARED_ONLY")
    opp_record = collector.read(opp / "version.info")
    opp_version = from_record(opp_record, "Version", confidence="DECLARED_ONLY")
    profiler = shutil.which("msprof") or str(root / "tools/profiler/bin/msprof")
    profiler_version = collector.run([profiler, "--version"])
    profiler_help = collector.run([profiler, "--help"])
    msprof = fact(status=profiler_version["execution_status"], reason=profiler_version["reason"] or "no recognized version format", source=[profiler_version["evidence_id"]])
    if msprof["status"] == "KNOWN":
        match = re.search(r"(?im)^\s*(?:msprof\s+)?version\s*[:=]?\s*(\d+[^\s]*)\s*$", profiler_version["stdout"])
        msprof = fact(match[1], "KNOWN", None, [profiler_version["evidence_id"]], "VERIFIED") if match else fact(status="PARSE_ERROR", reason="unrecognized msprof version format", source=[profiler_version["evidence_id"]])
    fingerprints = {}
    provider_files=[('cann-'+name,root/'lib64'/('lib'+name+'.so')) for name in ('opapi','nnopbase','ascendcl','msprofiler')]
    for name, path in [("msprof", Path(profiler).resolve()), ("cann-opp-version", opp / "version.info")]+provider_files:
        try:
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            record = collector.record(["sha256", str(path)], sha)
            record["parse_status"] = "KNOWN"
            fingerprints[name] = dict(path=collector.sanitize(str(path)), sha256=sha, evidence_id=record["evidence_id"])
        except OSError as exc:
            collector.record(["sha256", str(path)], stderr=str(exc), exit_code=None,
                             status="PERMISSION_DENIED" if isinstance(exc, PermissionError) else "NOT_FOUND")
    package_rows, package_paths = [], {}
    for name in PACKAGES:
        version, path = _package(collector, name)
        package_rows.append(dict(name=name, version=version))
        package_paths[name] = path
    packages = {p["name"]: p["version"] for p in package_rows}
    library_roots = json.loads(os.environ.get("KERNELX_LIBRARY_ROOTS", "{}"))
    if not isinstance(library_roots, dict) or any(name not in LIBRARIES or not isinstance(path, str) or not path for name, path in library_roots.items()):
        raise ValueError("KERNELX_LIBRARY_ROOTS must map supported library names to source repository paths")
    libraries, library_provenance = [], {}
    for name in LIBRARIES:
        package_name = PACKAGE_ALIASES.get(name,name)
        version = opp_version if name == "cann-opp" else packages[package_name]
        provenance = _library_provenance(collector, package_name, library_roots.get(name))
        library_provenance[name] = provenance
        libraries.append(dict(name=name, role="kernel_provider", version=version,
                              resolved_path=collector.sanitize(str(opp)) if name == "cann-opp" else package_paths[package_name],
                              package_id=provenance["package_id"], repository_url=provenance["repository_url"], git_commit=provenance["git_commit"], dirty_tree_sha256=None,
                              artifact_sha256=None, load_status="DECLARED_ONLY" if version["status"] == "KNOWN" else "NOT_FOUND", used_by_case=[]))
    conflicts = []
    if toolkit["value"] and opp_version["value"] and toolkit["value"] != opp_version["value"]:
        conflicts.append(dict(fields=["toolkit", "cann-opp"], reason="legacy toolkit install metadata and ops version differ; neither proves runtime provider"))
    python_record = collector.record(["python", "version"], platform.python_version())
    python_record["parse_status"] = "KNOWN"
    software = dict(npu_smi=smi_version, driver=from_record(driver_record, "Version"), firmware=from_record(firmware_record, "Version"),
                    toolkit=toolkit, msprof=msprof,
                    python=fact(platform.python_version(), "KNOWN", None, [python_record["evidence_id"]], "VERIFIED"),
                    search_paths={k: collector.sanitize(os.environ.get(k, "")) for k in
                                  ("ASCEND_HOME_PATH", "ASCEND_TOOLKIT_HOME", "ASCEND_OPP_PATH", "ASCEND_CUSTOM_OPP_PATH", "LD_LIBRARY_PATH", "PYTHONPATH")},
                    packages=package_rows, operator_libraries=libraries)
    matrix = []
    groups = {(d["soc_family"]["value"], d["hardware_bin"]["value"]) for d in devices} or {(None, None)}
    for soc, bin_name in sorted(groups, key=str):
        for lib in libraries:
            matrix.append(dict(library=lib["name"], revision=lib["version"]["value"] or lib["git_commit"], soc=soc, hardware_bin=bin_name,
                               cann=toolkit["value"], python=software["python"]["value"], torch_npu=packages["torch-npu"]["value"],
                               preset="latency-v1", status="UNVERIFIED", reason="read-only inventory; runtime revision, preset attribution and case support require runner verification",
                               verified_at=None, evidence_ids=lib["version"]["source"] + [profiler_help["evidence_id"]]))
    snapshot = dict(schema_version=1, protocol_version="latency-v1", environment_id=str(uuid.uuid4()),
                    server_id=collector.server_id, captured_at=now(), parser_version=PARSER_VERSION, redacted=redact,
                    host=host, device_inventory=inventory, devices=devices, software=software,
                    support_matrix=matrix, evidence=collector.evidence,
                    extensions=dict(bin_mapping_source=BIN_MAPPING_SOURCE, identity_version="server-die-sha256-v1", fingerprints=fingerprints, version_conflicts=conflicts,
                                    profiler_flags=sorted(set(re.findall(r"--[a-z][a-z-]+", profiler_help["stdout"]))) if profiler_help["execution_status"] == "KNOWN" else [],
                                    runtime_loading="DECLARED_ONLY: no benchmark process or NPU runtime initialized"))
    snapshot["extensions"]["library_provenance"] = library_provenance
    if os.environ.get('KERNELX_RELEASE_ID'):
        snapshot['extensions']['release']=dict(release_id=os.environ['KERNELX_RELEASE_ID'],git_commit=os.environ.get('KERNELX_GIT_COMMIT'))
    validate("environment", snapshot)
    return snapshot
