"""Read-only, bounded evidence collection. Never initializes torch/NPU runtimes."""
import getpass
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
from datetime import datetime, timezone
from pathlib import Path

from .protocol import digest, validate

PARSER_VERSION = "910b1-probe-v1"
# Full names observed in both mapping and board output on 910B1. No suffix guess.
BIN_MAPPING = {"Ascend 910B1": ("Ascend 910B", "Ascend 910B1"),
               "910B1": ("Ascend 910B", "Ascend 910B1")}
BIN_MAPPING_VERSION = "910b1-observed-v1"
LIBRARIES = ("cann-opp", "ops-transformer", "sgl-kernel-npu", "tile-kernels", "deepgemm-ascend", "deepep-ascend")
PACKAGES = ("torch", "torch-npu", "sgl-kernel-npu", "tile-kernels", "deepgemm-ascend", "deepep-ascend", "ops-transformer")


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
    for line in record["stdout"].splitlines():
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(.+?)\s*$", line)
        if match:
            rows.append(dict(npu_id=int(match[1]), chip_id=int(match[2]), logical_id=int(match[3]), chip_name_raw=match[4]))
    record["parse_status"] = "KNOWN" if rows else "PARSE_ERROR"
    if not rows:
        record["reason"] = "No accelerator rows in npu-smi mapping; MCU rows are excluded"
    return rows


class Collector:
    def __init__(self, server_id, redact=True, timeout=15):
        self.server_id = str(uuid.UUID(server_id))
        self.redact = redact
        self.timeout = timeout
        self.evidence = []

    def sanitize(self, text):
        if not self.redact:
            return text
        # Host-local identifiers are pseudonymized before serialization or hashing.
        def redact_identity(match):
            value = match[2].strip()
            if value.startswith("redacted:"):
                return match[1] + value
            if value.upper() in ("NA", "N/A", "UNKNOWN") or not re.search(r"[1-9A-Fa-f]", value):
                return match[1] + "NA"
            return match[1] + "redacted:" + hashlib.sha256((self.server_id + value).encode()).hexdigest()
        text = re.sub(r"(?im)^(\s*(?:VDie ID|NDie ID|Die ID|Serial Number|Serial No\.?|SN)\s*:\s*)(.+)$", redact_identity, text)
        hostname = socket.gethostname()
        if hostname:
            text = text.replace(hostname, "<hostname>")
        home = str(Path.home())
        if home != "/":
            text = text.replace(home, "/home/<user>")
        text = re.sub(r"/home/[^/\s]+", "/home/<user>", text)
        text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<ip>", text)
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
        die = data.get("VDie ID") or data.get("Die ID")
        if die and (not die.startswith("redacted:")) and (not re.search(r"[1-9A-Fa-f]", die) or die.upper() in ("NA", "N/A")):
            die = None
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
    for name, path in (("msprof", Path(profiler).resolve()), ("cann-opp-version", opp / "version.info")):
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
    libraries = []
    for name in LIBRARIES:
        version = opp_version if name == "cann-opp" else packages[name]
        libraries.append(dict(name=name, role="kernel_provider", version=version,
                              resolved_path=collector.sanitize(str(opp)) if name == "cann-opp" else package_paths[name],
                              package_id=None, repository_url=None, git_commit=None, dirty_tree_sha256=None,
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
            matrix.append(dict(library=lib["name"], revision=lib["version"]["value"], soc=soc, hardware_bin=bin_name,
                               cann=toolkit["value"], python=software["python"]["value"], torch_npu=packages["torch-npu"]["value"],
                               preset="latency-v1", status="UNVERIFIED", reason="read-only inventory; runtime revision, preset attribution and case support require runner verification",
                               verified_at=None, evidence_ids=lib["version"]["source"] + [profiler_help["evidence_id"]]))
    snapshot = dict(schema_version=1, protocol_version="latency-v1", environment_id=str(uuid.uuid4()),
                    server_id=collector.server_id, captured_at=now(), parser_version=PARSER_VERSION, redacted=redact,
                    host=host, device_inventory=inventory, devices=devices, software=software,
                    support_matrix=matrix, evidence=collector.evidence,
                    extensions=dict(fingerprints=fingerprints, version_conflicts=conflicts,
                                    profiler_flags=sorted(set(re.findall(r"--[a-z][a-z-]+", profiler_help["stdout"]))) if profiler_help["execution_status"] == "KNOWN" else [],
                                    runtime_loading="DECLARED_ONLY: no benchmark process or NPU runtime initialized"))
    validate("environment", snapshot)
    return snapshot
