#!/usr/bin/python3
"""Initialize a new guest once, then leave its persistent configuration alone."""

import json
import os
import re
import subprocess
import uuid
from pathlib import Path


def run(*args):
    subprocess.run(args, check=True)


run("/usr/local/libexec/incus-agent-setup")
marker = Path("/var/lib/koi/initialized")
if not marker.exists():
    seed = json.loads(Path("/run/incus_agent/files/koi.json.out").read_text())
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", seed["name"]):
        raise ValueError("Invalid guest name")
    if not re.fullmatch(r"[0-9a-f]{32}", seed["id"]):
        raise ValueError("Invalid VM identity")
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+", seed["ca"]):
        raise ValueError("Invalid guest access CA")
    run("groupadd", "--gid", "1000", "dev")
    run(
        "useradd",
        "--uid",
        "1000",
        "--gid",
        "dev",
        "--create-home",
        "--shell",
        "/bin/bash",
        "dev",
    )
    run("usermod", "--password", "*", "dev")
    ssh = Path("/home/dev/.ssh")
    ssh.mkdir(mode=0o700)
    (ssh / "authorized_keys").write_text("")
    (ssh / "authorized_keys").chmod(0o600)
    run("chown", "-R", "dev:dev", str(ssh), "/nix")
    Path("/etc/ssh/koi-ca.pub").write_text(seed["ca"] + "\n")
    Path("/etc/ssh/koi-principals").write_text("koi-" + seed["id"] + "\n")
    Path("/etc/ssh/koi-ca.pub").chmod(0o644)
    Path("/etc/ssh/koi-principals").chmod(0o644)
    Path("/etc/sudoers.d/dev").write_text("dev ALL=(ALL) NOPASSWD: ALL\n")
    Path("/etc/sudoers.d/dev").chmod(0o440)
    Path("/etc/hostname").write_text(seed["name"] + "\n")
    Path("/etc/hosts").write_text(
        "127.0.0.1 localhost\n127.0.1.1 " + seed["name"] + "\n"
    )
    Path("/etc/machine-id").write_text(uuid.uuid4().hex + "\n")
    run(
        "ssh-keygen",
        "-q",
        "-t",
        "ed25519",
        "-N",
        "",
        "-f",
        "/etc/ssh/ssh_host_ed25519_key",
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
run("hostname", Path("/etc/hostname").read_text().strip())
disk_sectors = Path("/sys/class/block/sda/size").read_text()
last_size = Path("/var/lib/koi/disk-sectors")
if not last_size.exists() or last_size.read_text() != disk_sectors:
    grown = subprocess.run(["growpart", "/dev/sda", "1"], check=False)
    if grown.returncode not in (0, 1):
        grown.check_returncode()
    run("resize2fs", "/dev/sda1")
    last_size.write_text(disk_sectors)
if not os.path.ismount("/sys/fs/cgroup"):
    run("mount", "-t", "cgroup2", "none", "/sys/fs/cgroup")
