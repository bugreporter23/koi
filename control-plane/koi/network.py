"""Koi-owned private guest subnet and local name projection."""

import ipaddress
import json
from pathlib import Path

from .common import atomic, run


def select_local_prefix():
    """Choose a private /24 disjoint from host routes and existing Incus claims."""
    routes = json.loads(run("ip", "-j", "-4", "route", "show", "table", "all").stdout)
    occupied = [
        ipaddress.IPv4Network(item["dst"], strict=False)
        for item in routes
        if item.get("dst") not in (None, "default", "0.0.0.0/0")
    ]
    networks = json.loads(
        run(
            "incus",
            "--force-local",
            "--project",
            "default",
            "network",
            "list",
            "--format=json",
        ).stdout
    )
    for item in networks:
        address = item.get("config", {}).get("ipv4.address", "none")
        if address not in ("none", "auto", ""):
            occupied.append(ipaddress.IPv4Network(address, strict=False))
    for pool in ("10.203.0.0/16", "172.30.0.0/16", "192.168.0.0/16"):
        for candidate in ipaddress.IPv4Network(pool).subnets(new_prefix=24):
            if not any(candidate.overlaps(existing) for existing in occupied):
                return str(candidate)
    raise ValueError(
        "No free local /24 in Koi's candidate ranges; inspect host networking"
    )


def status(c):
    interface = c["bridge"]
    result = run("ip", "-j", "link", "show", "dev", interface, check=False)
    return {
        "interface": interface,
        "prefix": c["prefix"],
        "present": result.returncode == 0,
        "reachability": "not observed",
    }


def hostname(c, guest):
    return f"{c['host']}.{guest['name']}"


def local_hosts(c, guests, path=Path("/etc/hosts")):
    marker = "# koi"
    retained = [
        line for line in path.read_text().splitlines() if not line.endswith(marker)
    ]
    records = [f"{g['address']} {hostname(c, g)} {marker}" for g in guests]
    atomic(path, "\n".join(retained + records) + "\n", path.stat().st_mode & 0o777)
