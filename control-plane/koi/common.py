import ipaddress
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


class Refused(Exception):
    pass


def require(ok, message):
    if not ok:
        raise Refused(message)


def token(value, pattern=r"[a-z][a-z0-9-]{0,30}"):
    require(
        isinstance(value, str) and re.fullmatch(pattern, value), "invalid identifier"
    )
    return value


def integer(value, low, high):
    require(type(value) is int and low <= value <= high, "integer outside envelope")
    return value


def run(*args, input=None, check=True):
    try:
        p = subprocess.run(
            args, input=input, text=True, capture_output=True, check=False, timeout=180
        )
    except subprocess.TimeoutExpired as e:
        raise Refused(f"{args[0]} timed out; inspect the operation before retry") from e
    if check and p.returncode:
        raise Refused(f"{args[0]} failed: {p.stderr[-2000:]}")
    return p


def atomic(path, value, mode=0o600):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), mode)
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        d = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(d)
        finally:
            os.close(d)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def config(path="/etc/koi/host.json"):
    c = json.loads(Path(path).read_text())
    require(c["networkMode"] == "local", "Koi requires local networking")
    integer(c["maxPoolPercent"], 1, 95)
    integer(c["mtu"], 576, 1500)
    token(c["host"])
    token(c["pool"])
    net = ipaddress.IPv4Network(c["prefix"])
    require(net.prefixlen == 24, "local guests require a /24 subnet")
    token(c["bridge"], r"[a-zA-Z0-9_.-]{1,15}")
    for fp in c["images"].values():
        token(fp, r"[0-9a-f]{64}")
    for size in c["sizes"].values():
        integer(size["cpu"], 1, 64)
        integer(size["memoryMiB"], 256, 262144)
        integer(size["diskGiB"], 4, 4096)
    return c
