"""Root-owned remote management destinations, atomically supplied as one document."""

import ipaddress
import json
from pathlib import Path
import re

from .common import atomic, integer, require, token
from .guest_access import public_key

PATH = Path("/var/lib/koi/admin-targets.json")


def validate(value):
    require(
        isinstance(value, dict) and len(value) <= 256, "expected named admin targets"
    )
    targets = {}
    for name, target in value.items():
        token(name)
        require(name != "local", "local is reserved")
        require(
            isinstance(target, dict)
            and set(target) == {"host", "port", "identityFile", "serverKey"},
            "target requires host, port, identityFile and serverKey",
        )
        host = target["host"]
        require(isinstance(host, str) and len(host) <= 253, "invalid target host")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            require(
                re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", host), "invalid target host"
            )
        identity = target["identityFile"]
        require(
            isinstance(identity, str)
            and Path(identity).is_absolute()
            and not any(c in identity for c in "\n\r\0"),
            "identityFile must be an absolute credential path",
        )
        targets[name] = {
            "host": host,
            "port": integer(target["port"], 1, 65535),
            "identityFile": identity,
            "serverKey": public_key(target["serverKey"]),
        }
    return targets


def install(value, *, check=False):
    targets = validate(value)
    if check:
        require(PATH.exists(), "admin targets are not installed")
        require(json.loads(PATH.read_text()) == targets, "admin targets differ")
    else:
        PATH.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        atomic(PATH, json.dumps(targets, sort_keys=True) + "\n")
    return {"verified": True, "targets": sorted(targets)}


def withdraw(identity_directory):
    directory = Path(identity_directory)
    require(
        directory.is_absolute(),
        "expected an absolute credential directory",
    )
    directory = directory.resolve()
    require(
        directory != Path("/"), "credential directory must not be the filesystem root"
    )
    if not PATH.exists():
        return {"verified": True, "targets": []}
    targets = validate(json.loads(PATH.read_text()))
    retained = {
        name: value
        for name, value in targets.items()
        if not Path(value["identityFile"]).resolve().is_relative_to(directory)
    }
    return install(retained)
