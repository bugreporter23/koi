"""Explicit trusted build-VM operation; separate from ordinary VM management."""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from koi.common import Refused, require, run, token
from koi.incus import Incus
from koi.storage import validate_pool


def progress(completed, message):
    bar = "█" * completed + "░" * (8 - completed)
    line = f"[{bar}] {completed}/8  {message}"
    if (
        sys.stderr.isatty()
        and os.environ.get("TERM") != "dumb"
        and "NO_COLOR" not in os.environ
    ):
        line = "\033[1;36m" + line + "\033[0m"
    print("\n" + line, file=sys.stderr, flush=True)


def run_live(*args):
    """Keep installation output visible; stdout remains reserved for the result."""
    try:
        result = subprocess.run(args, stdout=sys.stderr, check=False, timeout=180)
    except subprocess.TimeoutExpired as error:
        raise Refused(
            f"{args[0]} timed out; inspect the operation before retry"
        ) from error
    require(
        result.returncode == 0,
        f"{args[0]} failed (exit {result.returncode}); see output above",
    )


def timed_progress():
    started = previous = time.monotonic()

    def advance(completed, message):
        nonlocal previous
        now = time.monotonic()
        if completed:
            print(
                f"Stage: {now - previous:.1f}s; total: {now - started:.1f}s",
                file=sys.stderr,
                flush=True,
            )
        previous = now
        progress(completed, message)

    return advance


def prepare_guest(inputs, pool, prepare=None, advance=progress, *, disk_gib=5):
    """Build and clean a running guest; the caller owns export and shutdown."""
    require(os.geteuid() == 0, "trusted host administrator required")
    inputs = Path(inputs)
    pool = token(pool)
    base = json.loads((inputs / "base.json").read_text())
    api = Incus()
    validate_pool(api.request("GET", "/1.0/storage-pools/" + pool))
    images = api.request("GET", "/1.0/images?recursion=1")
    if any(image["fingerprint"] == base["fingerprint"] for image in images):
        advance(0, "Reusing pinned base image")
    else:
        advance(0, "Importing base image")
        run_live(
            "incus",
            "image",
            "import",
            str(inputs / "incus.tar.xz"),
            str(inputs / "disk.qcow2"),
        )
    name = "koi-build-" + uuid.uuid4().hex[:12]
    print(
        "Build VM: " + name + "; failures leave it for explicit inspection",
        file=sys.stderr,
        flush=True,
    )
    advance(1, "Creating builder VM")
    api.request(
        "POST",
        "/1.0/instances",
        {
            "name": name,
            "type": "virtual-machine",
            "profiles": [],
            "source": {"type": "image", "fingerprint": base["fingerprint"]},
            "config": {
                "limits.cpu": "1",
                "limits.memory": "768MiB",
                "security.secureboot": "false",
                "cloud-init.user-data": "#cloud-config\nmanage_etc_hosts: true\n",
                "cloud-init.network-config": '{"version":1,"config":[]}',
            },
            "devices": {
                "root": {
                    "type": "disk",
                    "path": "/",
                    "pool": pool,
                    "size": f"{disk_gib}GiB",
                },
                "cloud-init": {"type": "disk", "source": "cloud-init:config"},
            },
        },
    )
    metadata_path = "/1.0/instances/" + name + "/metadata"
    metadata = api.request("GET", metadata_path)
    metadata["templates"] = {
        path: template
        for path, template in metadata["templates"].items()
        if not path.startswith("/var/lib/cloud/")
    }
    api.request("PUT", metadata_path, metadata)
    advance(2, "Booting builder and waiting for guest initialization")
    api.request("PUT", "/1.0/instances/" + name + "/state", {"action": "start"})
    command = ["incus", "exec", "--disable-stdin", "--force-noninteractive", name, "--"]
    deadline = time.monotonic() + 180
    while run(*command, "true", check=False).returncode:
        require(time.monotonic() < deadline, "build VM agent unavailable")
        time.sleep(1)
    run_live(
        *command,
        "systemctl",
        "mask",
        "--runtime",
        "--now",
        "systemd-networkd-wait-online.service",
    )
    run_live(*command, "cloud-init", "status", "--wait")
    advance(3, "Installing guest packages")
    run_live(
        "incus",
        "file",
        "push",
        str(inputs / "packages.tar"),
        name + "/tmp/packages.tar",
    )
    run_live(
        *command,
        "sh",
        "-c",
        "mkdir /tmp/koi-packages; tar -xf /tmp/packages.tar -C /tmp/koi-packages",
    )
    run_live(
        *command,
        "sh",
        "-c",
        "DEBIAN_FRONTEND=noninteractive apt-get -y --no-install-recommends "
        "-o Dir::Etc::sourcelist=/dev/null -o Dir::Etc::sourceparts=- "
        "-o Dpkg::Options::=--force-confold install /tmp/koi-packages/*.deb 1>&2",
    )
    run_live(
        *command,
        "sh",
        "-c",
        "command -v git; command -v tmux; command -v curl; test -x /usr/sbin/sshd; systemctl enable ssh",
    )
    advance(4, "Preparing guest runtime")
    if prepare is not None:
        prepare(name)
    advance(5, "Cleaning guest identities and package files")
    run_live(
        *command,
        "sh",
        "-c",
        "cloud-init clean --logs --machine-id --seed; rm -f /etc/ssh/ssh_host_*; rm -rf /root/.ssh /tmp/koi-packages /tmp/packages.tar; rm -f /var/cache/apt/archives/*.deb",
    )
    return {"builder": name, "source": base["fingerprint"]}


def build(inputs, pool, prepare=None):
    """Prepare an offline base, optionally customize it, then clean and publish."""
    advance = timed_progress()
    result = prepare_guest(inputs, pool, prepare, advance)
    name = result["builder"]
    api = Incus()
    advance(6, "Stopping builder VM")
    api.request(
        "PUT", "/1.0/instances/" + name + "/state", {"action": "stop", "timeout": 60}
    )
    advance(7, "Publishing reusable image")
    published = api.request(
        "POST",
        "/1.0/images",
        {
            "source": {"type": "instance", "name": name},
            "properties": {
                "description": "Koi Debian 13 SSH/Git/tmux/curl; pinned offline packages",
                "requirements.cdrom_cloud_init": "true",
            },
        },
    )
    fingerprint = token(published["metadata"]["fingerprint"], r"[0-9a-f]{64}")
    advance(8, "Image ready")
    return {**result, "fingerprint": fingerprint}


def main():
    p = argparse.ArgumentParser(
        prog="koi-build-image",
        description="Build/publish a stopped Debian image using pinned offline inputs",
    )
    p.add_argument("inputs", type=Path)
    p.add_argument(
        "pool",
        help="existing native Incus ZFS pool",
    )
    args = p.parse_args()
    print(json.dumps(build(args.inputs, args.pool)))


if __name__ == "__main__":
    main()
