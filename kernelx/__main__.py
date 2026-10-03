import argparse
import json
from pathlib import Path

from .probe import probe
from .protocol import case_key, validate


def main():
    parser = argparse.ArgumentParser(prog="kernelx")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("probe", help="read-only environment snapshot")
    scan.add_argument("--server-id", required=True, help="registered server UUID, not logical device ID")
    scan.add_argument("--output", type=Path, required=True)
    scan.add_argument("--timeout", type=float, default=15)
    scan.add_argument("--include-identities", action="store_true", help="disable default redaction for private local evidence")
    check = commands.add_parser("validate")
    check.add_argument("entity", choices=["case", "plan", "session", "attempt", "artifact", "profile", "observation", "environment"])
    check.add_argument("path", type=Path)
    key = commands.add_parser("case-key")
    key.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.command == "probe":
        if args.timeout <= 0:
            parser.error("timeout must be positive")
        snapshot = probe(args.server_id, not args.include_identities, args.timeout)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(dict(output=str(args.output), devices=len(snapshot["devices"]),
                              inventory_status=snapshot["device_inventory"]["status"])))
    else:
        value = json.loads(args.path.read_text())
        if args.command == "case-key":
            print(case_key(value))
        else:
            validate(args.entity, value)
            print("valid " + args.entity)


if __name__ == "__main__":
    main()
