"""Canonical keys and a dependency-free validator for our JSON Schema vocabulary.

Schemas also work with standard Draft 2020-12 JSON Schema validators. This small
validator deliberately supports only the keywords used by the bundled schemas.
"""
import hashlib
import json
import math
import re
from pathlib import Path


class ValidationError(ValueError):
    pass


def canonical_json(value):
    def check(item):
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Non-finite numbers are not protocol values")
        if isinstance(item, dict):
            if any(not isinstance(k, str) for k in item):
                raise ValueError("JSON keys must be strings")
            for v in item.values():
                check(v)
        elif isinstance(item, list):
            for v in item:
                check(v)
    check(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate(entity, value):
    root = json.loads((Path(__file__).parent / "schemas" / (entity + ".json")).read_text())

    def visit(schema, item, path):
        if "$ref" in schema:
            node = root
            for key in schema["$ref"].split("/")[1:]:
                node = node[key]
            visit(node, item, path)
            return
        if "anyOf" in schema:
            for choice in schema["anyOf"]:
                try:
                    visit(choice, item, path)
                    return
                except ValidationError:
                    pass
            raise ValidationError(path + ": no allowed variant")
        types = {"object": isinstance(item, dict), "array": isinstance(item, list),
                 "string": isinstance(item, str), "null": item is None,
                 "boolean": isinstance(item, bool),
                 "integer": isinstance(item, int) and not isinstance(item, bool),
                 "number": isinstance(item, (int, float)) and not isinstance(item, bool)
                 and (not isinstance(item, float) or math.isfinite(item))}
        if "type" in schema and not types[schema["type"]]:
            raise ValidationError(path + ": expected " + schema["type"])
        if "const" in schema and (item != schema["const"] or type(item) is not type(schema["const"])):
            raise ValidationError(path + ": wrong constant")
        if "enum" in schema and item not in schema["enum"]:
            raise ValidationError(path + ": invalid enum")
        if isinstance(item, dict):
            missing = set(schema.get("required", [])) - item.keys()
            if missing:
                raise ValidationError(path + ": missing " + ", ".join(sorted(missing)))
            props = schema.get("properties", {})
            if schema.get("additionalProperties") is False and item.keys() - props.keys():
                raise ValidationError(path + ": unexpected fields")
            for key, val in item.items():
                if key in props:
                    visit(props[key], val, path + "." + key)
        if isinstance(item, list):
            if len(item) < schema.get("minItems", 0):
                raise ValidationError(path + ": too few items")
            if schema.get("uniqueItems") and len({canonical_json(v) for v in item}) != len(item):
                raise ValidationError(path + ": duplicate items")
            for i, val in enumerate(item):
                visit(schema.get("items", {}), val, path + "[" + str(i) + "]")
        if isinstance(item, str):
            if len(item) < schema.get("minLength", 0):
                raise ValidationError(path + ": empty string")
            if "pattern" in schema and not re.search(schema["pattern"], item):
                raise ValidationError(path + ": pattern mismatch")
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            if item < schema.get("minimum", float("-inf")):
                raise ValidationError(path + ": below minimum")
    visit(root, value, entity)
    canonical_json(value)
    def semantics(item):
        if isinstance(item, dict):
            if set(item) == {"shape", "dtype", "layout", "stride"} and item["stride"] is not None:
                if len(item["shape"]) != len(item["stride"]):
                    raise ValidationError("stride rank must match shape rank")
            if set(item) == {"value", "status", "reason", "source", "confidence"}:
                if item["status"] == "KNOWN":
                    if item["value"] is None or item["confidence"] == "NONE":
                        raise ValidationError("known fact needs value and confidence")
                elif item["value"] is not None or not item["reason"] or item["confidence"] != "NONE":
                    raise ValidationError("unknown fact needs null value, reason and NONE confidence")
                if not item["source"]:
                    raise ValidationError("fact needs evidence source")
            if "quality" in item and "VALID" in item["quality"] and len(item["quality"]) != 1:
                raise ValidationError("VALID cannot accompany quality exclusions")
            for child in item.values():
                semantics(child)
        elif isinstance(item, list):
            for child in item:
                semantics(child)
    semantics(value)
    if entity == "environment":
        evidence = {e["evidence_id"]: e for e in value["evidence"]}
        if len(evidence) != len(value["evidence"]):
            raise ValidationError("duplicate evidence IDs")
        for entry in evidence.values():
            if entry["output_sha256"] != digest({"stdout": entry["stdout"], "stderr": entry["stderr"]}):
                raise ValidationError("evidence digest mismatch")
        def check_sources(item):
            if isinstance(item, dict):
                if set(item) == {"value", "status", "reason", "source", "confidence"}:
                    if set(item["source"]) - evidence.keys():
                        raise ValidationError("dangling fact evidence reference")
                for child in item.values():
                    check_sources(child)
            elif isinstance(item, list):
                for child in item:
                    check_sources(child)
        check_sources(value)
    if entity == "profile" and value["completeness"] == "COMPLETE":
        if value["actual_task_count"] != value["expected_task_count"]:
            raise ValidationError("complete profile must have expected task count")
    if entity == "observation" and "VALID" in value["quality"] and value["completeness"] != "COMPLETE":
        raise ValidationError("partial observation cannot be VALID")
    return value


def case_key(case):
    validate("case", case)
    return "case-v1:" + digest(case)


def comparison_key(case, environment, device, context):
    """Fail closed on unknown metadata; never match null BINs as a group."""
    validate("case", case)
    validate("environment", environment)
    if device not in environment["devices"]:
        raise ValidationError("device is not part of environment")
    for name in ("soc_family", "hardware_bin"):
        if device[name]["status"] != "KNOWN" or device[name]["confidence"] != "VERIFIED":
            raise ValidationError("unknown " + name)
    software = environment["software"]
    for name in ("driver", "firmware", "toolkit", "msprof"):
        if software[name]["status"] != "KNOWN" or software[name]["confidence"] != "VERIFIED":
            raise ValidationError("unknown " + name)
    required = ("runtime_library_sha256", "framework_sha256", "implementation_sha256",
                "preset_sha256", "parser_version", "protocol_version", "compile_options",
                "concurrency", "thermal_state", "communication")
    def has_null(item):
        if item is None:
            return True
        if isinstance(item, dict):
            return any(has_null(v) for v in item.values())
        if isinstance(item, list):
            return any(has_null(v) for v in item)
        return False
    if set(context) != set(required) or has_null(context):
        raise ValidationError("comparison context incomplete")
    if context["protocol_version"] != case["protocol_version"]:
        raise ValidationError("comparison protocol mismatch")
    for name in required[:4]:
        if not isinstance(context[name], str) or not re.fullmatch(r"[0-9a-f]{64}", context[name]):
            raise ValidationError("invalid comparison fingerprint: " + name)
    return "group-v1:" + digest({"case_key": case_key(case), "context": context,
                                 "hardware": {k: device[k]["value"] for k in
                                              ("soc_family", "hardware_bin")},
                                 "software": {k: software[k]["value"] for k in
                                              ("driver", "firmware", "toolkit", "msprof")}})
