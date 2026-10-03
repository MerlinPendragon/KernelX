"""Generate the checked-in Draft 2020-12 contracts; no third-party dependencies."""
import json
from pathlib import Path

S = {"type": "string", "minLength": 1}
I = {"type": "integer", "minimum": 0}
N = {"type": "number", "minimum": 0}
H = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
U = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}
T = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$"}

def enum(*items): return {"type": "string", "enum": list(items)}
def nullable(x): return {"anyOf": [x, {"type": "null"}]}
def arr(x, minimum=0): return {"type": "array", "items": x, "minItems": minimum}
def obj(**props): return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
def ref(x): return {"$ref": "#/$defs/" + x}

status = enum("KNOWN", "UNKNOWN", "PERMISSION_DENIED", "UNSUPPORTED", "PARSE_ERROR", "NOT_FOUND", "TIMEOUT", "COMMAND_FAILED")
fact = obj(value=nullable(S), status=status, reason=nullable(S), source=arr(S), confidence=enum("VERIFIED", "DECLARED_ONLY", "LOCATION_ONLY", "NONE"))
command = obj(evidence_id=S, argv=arr(S, 1), started_at=T, duration_seconds=N,
              exit_code=nullable({"type": "integer"}), stdout={"type": "string"}, stderr={"type": "string"},
              execution_status=status, parse_status=status, reason=nullable(S),
              permission_scope=S, parser_version=S, redacted={"type": "boolean"}, output_sha256=H)
device = obj(device_uid=S, card_uid=S, identity_confidence=enum("STABLE_CHIP", "LOCATION_ONLY"),
             card_identity_confidence=enum("LOCATION_ONLY"), npu_id=I, chip_id=I, logical_id=I,
             chip_name_raw=S, soc_family=ref("fact"), hardware_bin=ref("fact"),
             bin_mapping_version=S, board_id=ref("fact"), pcie=ref("fact"), hbm_mb=ref("fact"), firmware=ref("fact"))
library = obj(name=S, role=S, version=ref("fact"), resolved_path=nullable(S),
              package_id=nullable(S), repository_url=nullable(S), git_commit=nullable(S),
              dirty_tree_sha256=nullable(H), artifact_sha256=nullable(H),
              load_status=enum("DECLARED_ONLY", "VERIFIED", "NOT_FOUND"), used_by_case=arr(S))
support = obj(library=S, revision=nullable(S), soc=nullable(S), hardware_bin=nullable(S),
              cann=nullable(S), python=nullable(S), torch_npu=nullable(S), preset=S,
              status=enum("VERIFIED", "UNSUPPORTED", "UNVERIFIED"), reason=S,
              verified_at=nullable(T), evidence_ids=arr(S))
quality = arr(enum("VALID", "CONTENDED", "UNSTABLE", "THERMAL_DRIFT", "VERSION_CHANGED",
                   "METADATA_INCOMPLETE", "INSUFFICIENT_DATA", "ATTRIBUTION_UNKNOWN", "TRACE_MISSING", "UNIT_UNKNOWN"), 1)
tensor = obj(shape=arr(I), dtype=S, layout=S, stride=nullable(arr(I)))
entities = {
 "case": obj(operator=S, inputs=arr(tensor, 1), outputs=arr(tensor, 1), attributes={"type": "object"},
             input_generation=obj(algorithm=S, version=S, seed=I, input_sha256=nullable(H)),
             semantic_version=S, protocol_version={"const": "latency-v1"}),
 "plan": obj(plan_id=U, plan_sha256=H, case_manifest_sha256=H, policy_id=U, policy_version=I,
             valid_from=T, valid_until=T, case_keys=arr(S, 1), preset=S,
             budget_seconds=N, cleanup_reserve_seconds=N, priorities={"type": "object"}),
 "session": obj(session_id=U, server_id=U, device_uids=arr(S, 1), environment_id=U,
                plan_id=U, plan_sha256=H, release_id=S, policy_id=U, policy_version=I,
                window_id=S, started_at=T, ended_at=nullable(T),
                state=enum("RUNNING", "COMPLETED", "PARTIAL", "SKIPPED", "FAILED"),
                resource_ledger=arr(obj(device_uid=S, start_monotonic_ns=I, end_monotonic_ns=nullable(I))), reason=nullable(S)),
 "attempt": obj(attempt_id=U, session_id=U, task_id=U, case_key=S, ordinal=I,
                started_at=T, ended_at=nullable(T), state=enum("RUNNING", "SUCCEEDED", "REJECTED", "FAILED", "INTERRUPTED"),
                exit_code=nullable({"type": "integer"}), process_group=nullable(I),
                release_status=enum("NOT_CHECKED", "RELEASED", "RESIDUAL", "UNKNOWN"),
                released_at=nullable(T), evidence_ids=arr(S), reason=nullable(S)),
 "artifact": obj(artifact_id=U, uri=S, sha256=H, bytes=I,
                  kind=enum("RAW_PROF", "CSV", "TRACE", "SIDECAR", "LOG", "ENVIRONMENT"),
                  created_at=T, redacted={"type": "boolean"}, media_type=S),
 "profile": obj(profile_id=U, attempt_id=U, case_key=S, preset=S, preset_sha256=H,
                 collector_version=ref("fact"), exporter_version=ref("fact"), parser_version=S,
                 artifact_ids=arr(U, 1), final_argv=arr(S, 1),
                 completeness=enum("COMPLETE", "PARTIAL", "MISSING"),
                 export_status=status, attribution_status=status, expected_task_count=I,
                 actual_task_count=nullable(I), task_mapping=arr(obj(iteration=I, rank=I, task_ids=arr(S, 1))),
                 quality=quality, reason=nullable(S)),
 "observation": obj(observation_id=U, profile_id=U, attempt_id=U, session_id=U,
                     environment_id=U, device_uid=S, task_id=U, case_key=S,
                     input_sha256=nullable(H), seed=I, round=I, iteration=I, rank=I,
                     phase=enum("MEASURE"), task_ids=arr(S, 1),
                     metric=obj(name=enum("task_duration_us", "device_span_us", "device_critical_path_us", "host_elapsed_us", "rank_elapsed_us"),
                                unit={"const": "us"}, boundary=S, definition_version=S),
                     raw_samples=arr(N, 1), completeness=enum("COMPLETE", "PARTIAL", "MISSING"), quality=quality),
 "environment": obj(environment_id=U, server_id=U, captured_at=T, parser_version=S,
                     redacted={"type": "boolean"},
                     host=obj(hostname=ref("fact"), architecture=ref("fact"), kernel=ref("fact"), os=ref("fact"), cpu_numa=ref("fact")),
                     device_inventory=ref("fact"), devices=arr(ref("device")),
                     software=obj(npu_smi=ref("fact"), driver=ref("fact"), firmware=ref("fact"), toolkit=ref("fact"),
                                  msprof=ref("fact"), python=ref("fact"), search_paths={"type": "object"},
                                  packages=arr(obj(name=S, version=ref("fact"))),
                                  operator_libraries=arr(ref("library"))),
                     support_matrix=arr(ref("support")), evidence=arr(ref("command"), 1))
}
defs = {"fact": fact, "device": device, "library": library, "support": support, "command": command}
for name, schema in entities.items():
    # Every field is explicit; extensions are the sole additive metadata escape hatch.
    schema["properties"].update(schema_version={"const": 1}, extensions={"type": "object"})
    schema["required"] += ["schema_version", "extensions"]
    if name != "case":
        schema["properties"]["protocol_version"] = {"const": "latency-v1"}
        schema["required"].append("protocol_version")
    used = set()
    def references(node):
        if isinstance(node, dict):
            if "$ref" in node:
                key = node["$ref"].split("/")[-1]
                if key not in used:
                    used.add(key)
                    references(defs[key])
            for child in node.values():
                references(child)
        elif isinstance(node, list):
            for child in node:
                references(child)
    references(schema)
    schema.update({"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://kernelx.invalid/schema/v1/" + name, "$defs": {key: val for key, val in defs.items() if key in used}})
    path = Path(__file__).resolve().parents[1] / "kernelx" / "schemas" / (name + ".json")
    path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
