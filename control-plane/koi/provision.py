"""Initialize Koi-owned Incus storage, project and local network."""

import ipaddress
import json
from pathlib import Path
import re
import socket

from . import network
from .common import atomic, require, run, token

HOST = Path("/var/lib/koi/host.json")


def incus(*arguments, input=None):
    return run(
        "incus", "--force-local", "--project", "default", *arguments, input=input
    ).stdout


def initialize(storage_zfs):
    require(not HOST.exists(), "Koi is already configured; inspect before provisioning")
    require(
        re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)+", storage_zfs
        ),
        "Expected a dedicated ZFS dataset below an existing pool",
    )
    hostname = token(socket.gethostname().split(".")[0])
    pool = storage_zfs.split("/", 1)[0]
    datasets = run("zfs", "list", "-r", "-H", "-o", "name", pool).stdout.splitlines()
    require(
        not any(name.startswith(storage_zfs + "/") for name in datasets),
        "Use a dedicated empty dataset without children",
    )
    require(
        storage_zfs.rsplit("/", 1)[0] in datasets,
        "The selected dataset's parent must already exist",
    )
    if storage_zfs in datasets:
        run("zfs", "list", "-H", "-t", "filesystem", storage_zfs)
    run("systemctl", "start", "incus.service")
    for kind, name in (
        ("project", "koi"),
        ("storage", "koi"),
        ("network", "koi-local"),
    ):
        require(
            all(
                item["name"] != name
                for item in json.loads(incus(kind, "list", "--format=json"))
            ),
            "Refusing to reuse existing Incus " + kind + ": " + name,
        )
    prefix = ipaddress.IPv4Network(network.select_local_prefix())
    if storage_zfs not in datasets:
        run("zfs", "create", "-o", "mountpoint=none", storage_zfs)
    incus(
        "admin",
        "init",
        "--preseed",
        input=json.dumps(
            {
                "storage_pools": [
                    {
                        "name": "koi",
                        "driver": "zfs",
                        "config": {
                            "source": storage_zfs,
                            "zfs.clone_copy": "true",
                            "volume.zfs.reserve_space": "false",
                            "volume.zfs.remove_snapshots": "false",
                        },
                    }
                ],
                "networks": [
                    {
                        "name": "koi-local",
                        "type": "bridge",
                        "config": {
                            "ipv4.address": str(next(prefix.hosts())) + "/24",
                            "ipv4.nat": "true",
                            "ipv6.address": "none",
                            "dns.mode": "none",
                            "bridge.mtu": "1500",
                        },
                    }
                ],
                "projects": [
                    {
                        "name": "koi",
                        "config": {
                            "features.images": "false",
                            "features.profiles": "false",
                            "features.networks": "false",
                            "features.storage.volumes": "false",
                        },
                    }
                ],
            }
        ),
    )
    HOST.parent.mkdir(parents=True, exist_ok=True)
    atomic(
        HOST,
        json.dumps(
            {
                "host": hostname,
                "networkMode": "local",
                "bridge": "koi-local",
                "prefix": str(prefix),
                "mtu": 1500,
                "pool": "koi",
                "maxPoolPercent": 80,
                "images": {},
                "sizes": {"small": {"cpu": 2, "memoryMiB": 2048, "diskGiB": 20}},
            },
            indent=2,
        )
        + "\n",
    )
