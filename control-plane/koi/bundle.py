"""Validate a Koi v1 image bundle without granting access or executing payloads."""

import argparse
import json
import re
import sys
from pathlib import Path

FIELDS = {"version", "image", "architecture", "boot", "payloads", "requirements"}


def validate(value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("Expected a complete Koi bundle descriptor")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Unsupported Koi bundle contract")
    if value["architecture"] != "x86_64" or value["boot"] != "q35-runit-v3":
        raise ValueError("Unsupported guest architecture or boot contract")
    if not isinstance(value["image"], str) or not re.fullmatch(
        r"[a-z][a-z0-9-]{0,30}", value["image"]
    ):
        raise ValueError("Invalid image alias")
    if not isinstance(value["payloads"], list) or len(value["payloads"]) > 32:
        raise ValueError("Expected a bounded payload list")
    names = set()
    for payload in value["payloads"]:
        if not isinstance(payload, dict) or set(payload) != {"archive", "installer"}:
            raise ValueError("Expected archive and installer for each payload")
        for field, suffix in (("archive", ".tar"), ("installer", ".py")):
            name = payload[field]
            if not isinstance(name, str) or not re.fullmatch(
                r"[a-z][a-z0-9-]*" + re.escape(suffix), name
            ):
                raise ValueError("Payload files must be named local inputs")
            if name in names:
                raise ValueError("Duplicate payload file")
            names.add(name)
    if not isinstance(value["requirements"], list) or len(value["requirements"]) > 32:
        raise ValueError("Expected a bounded connection requirement list")
    names = set()
    for requirement in value["requirements"]:
        if not isinstance(requirement, dict) or set(requirement) != {
            "name",
            "direction",
            "service",
            "protocol",
        }:
            raise ValueError("Unsupported connection requirement fields")
        if (
            requirement["direction"] != "guest-to-service"
            or requirement["protocol"] != "tcp"
        ):
            raise ValueError("Unsupported connection requirement semantics")
        for field in ("name", "service"):
            name = requirement[field]
            if not isinstance(name, str) or not re.fullmatch(
                r"[a-z][a-z0-9-]{0,30}", name
            ):
                raise ValueError("Invalid connection requirement name")
        if requirement["name"] in names:
            raise ValueError("Duplicate connection requirement name")
        names.add(requirement["name"])
    return value


def main():
    parser = argparse.ArgumentParser(prog="koi-bundle-check", description=__doc__)
    parser.add_argument("descriptor", help="JSON file or - for stdin")
    args = parser.parse_args()
    raw = (
        sys.stdin.buffer.read(65537)
        if args.descriptor == "-"
        else Path(args.descriptor).read_bytes()
    )
    if len(raw) > 65536:
        raise ValueError("Bundle descriptor is too large")
    print(json.dumps(validate(json.loads(raw)), sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print("koi-bundle-check: " + str(error), file=sys.stderr)
        sys.exit(1)
