"""Compute initialization, explicit image selection and guest access."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from koi.timing import stage

HOST = Path("/var/lib/koi/host.json")


def run(*command, capture=False):
    result = subprocess.run(
        command, text=True, stdout=subprocess.PIPE if capture else None, check=True
    )
    return result.stdout


def state():
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    if not base.is_absolute():
        raise ValueError("XDG_STATE_HOME must be absolute")
    if any(c in str(base) for c in ('"', "\\", "\n", "\r", "\x00")):
        raise ValueError("State path contains unsupported SSH configuration characters")
    return base / "koi"


def initialize(root):
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    root.chmod(0o700)
    print("Local Koi state: " + str(root))


def management(operation, *arguments):
    with stage("management." + operation):
        return json.loads(
            run(
                "koi",
                operation,
                *arguments,
                "--json",
                capture=True,
            )
        )


def named_guest(name, *, required=True):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", name):
        raise ValueError("Expected a VM name such as vm1")
    matches = [guest for guest in management("list") if guest["name"] == name]
    if not matches:
        if required:
            raise ValueError("No VM named " + name + "; see koi list")
        return None
    if len(matches) != 1:
        raise ValueError("Ambiguous VM name: " + name)
    return matches[0]


def guest_binding(root, name):
    guest = named_guest(name)
    directory = root / "ssh"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    binding = directory / (guest["id"] + ".conf")
    management(
        "bind",
        guest["id"],
        str(binding),
    )
    return str(binding)


def copy_guest(root, source, destination):
    paths = [source, destination]
    remote = [
        (index, match)
        for index, path in enumerate(paths)
        if (match := re.fullmatch(r"([a-z][a-z0-9-]{0,30}):(.*)", path, re.DOTALL))
    ]
    if len(remote) != 1:
        raise ValueError(
            "Use one local path and one VM:path, for example vm2:/home/dev/"
        )
    index, match = remote[0]
    binding = guest_binding(root, match[1])
    paths[1 - index] = str(Path(paths[1 - index]).expanduser().absolute())
    os.execvp("scp", ["scp", "-r", "-F", binding, "--", *paths])


def initialize_host(storage_zfs):
    run("systemctl", "--no-pager", "cat", "koi.service")
    if not HOST.exists():
        run("sudo", "koi-host", "initialize", "--storage-zfs", storage_zfs)
    run("sudo", "systemctl", "start", "koi.service")
    print("Local Koi ready")


def add_commands(sub):
    initialization = sub.add_parser(
        "init", help="Initialize compute storage, network and daemon"
    )
    initialization.add_argument(
        "--storage-zfs",
        required=True,
        help="Dedicated ZFS dataset below an existing parent",
    )
    image = sub.add_parser(
        "build-image", help="Build explicit image inputs; select them for future guests"
    )
    image.add_argument("inputs", type=Path)
    image.add_argument("--pool", default="koi")
    image.add_argument(
        "--output", type=Path, help="Build artifacts without importing them"
    )
    up = sub.add_parser(
        "up", help="Create and start a VM from an explicitly selected image"
    )
    up.add_argument("name")
    up.add_argument("--image", required=True, help="Configured image alias")
    up.add_argument("--size", default="small")
    ssh = sub.add_parser("ssh", help="SSH into a named local VM")
    ssh.add_argument("name")
    ssh.add_argument("command", nargs=argparse.REMAINDER)
    copy = sub.add_parser("cp", help="Copy a local path to or from VM:path")
    copy.add_argument("source")
    copy.add_argument("destination")
    for name in ("init", "build-image", "up", "ssh", "cp"):
        sub.choices[name].set_defaults(_execute=execute)


def execute(args):
    if args.host != "local":
        raise ValueError(
            "Workstation operations require --host local; use bind or exec-guest for remote access"
        )
    root = state()
    if args.op == "init":
        initialize_host(args.storage_zfs)
        initialize(root)
    elif args.op == "build-image":
        command = ["sudo", os.environ["KOI_IMAGE_BUILDER"], str(args.inputs.absolute())]
        command += (
            ["--output", str(args.output.absolute())]
            if args.output is not None
            else [args.pool]
        )
        run(*command)
    elif args.op == "up":
        guest = management(
            "create", args.name, "--image", args.image, "--size", args.size
        )
        management("start", guest["id"])
        print("Started " + args.name)
    elif args.op == "ssh":
        binding = guest_binding(root, args.name)
        os.execvp("ssh", ["ssh", "-F", binding, "--", args.name, *args.command])
    elif args.op == "cp":
        copy_guest(root, args.source, args.destination)
