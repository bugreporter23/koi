"""Assemble a Debian/runit filesystem offline and publish its direct-boot disk."""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from koi.bundle import validate as validate_bundle
from koi.common import Refused, atomic, require, run


def live(*command):
    subprocess.run(command, stdout=sys.stderr, check=True)


def artifacts(inputs, destination):
    require(os.geteuid() == 0, "trusted host administrator required")
    inputs = Path(inputs)
    bundle = validate_bundle(json.loads((inputs / "bundle.json").read_text()))
    for payload in bundle["payloads"]:
        for name in payload.values():
            require((inputs / name).is_file(), "Missing bundle input: " + name)
    destination = Path(destination)
    destination.mkdir()
    root = destination / "root"
    packages = json.loads((inputs / "packages.json").read_text())
    names = [package["name"].split("_", 1)[0] for package in packages]
    print("Assembling pinned Debian filesystem offline", file=sys.stderr, flush=True)
    live(
        "debootstrap",
        "--arch=amd64",
        "--variant=minbase",
        "--foreign",
        "--no-check-gpg",
        "--include=" + ",".join(names),
        "trixie",
        str(root),
        "file://" + str((inputs / "debian").resolve()),
    )
    live("mount", "--rbind", "/dev", str(root / "dev"))
    try:
        live(
            "env",
            "DEBOOTSTRAP_DIR=" + str(root / "debootstrap"),
            "debootstrap",
            "--second-stage",
            "--second-stage-target=" + str(root),
        )
        (root / "nix").mkdir(exist_ok=True)
        for payload in bundle["payloads"]:
            live(
                "tar",
                "--no-same-owner",
                "-xf",
                str(inputs / payload["archive"]),
                "-C",
                str(root),
            )
            live(
                sys.executable,
                str(inputs / payload["installer"]),
                str(root),
                str(inputs),
            )
        (root / "usr/local/libexec").mkdir(parents=True, exist_ok=True)
        for source, target in (
            ("first-boot.py", "usr/local/libexec/koi-first-boot"),
            ("prepare-agent.sh", "usr/local/libexec/koi-prepare-agent"),
            ("stage1.sh", "etc/runit/1"),
            ("incus-agent-setup", "usr/local/libexec/incus-agent-setup"),
            ("prepare-image.sh", "tmp/prepare-image.sh"),
        ):
            shutil.copyfile(inputs / source, root / target)
        (root / "usr/local/libexec/incus-agent-setup").chmod(0o755)
        live(
            "tar",
            "--no-same-owner",
            "-xf",
            str(inputs / "kernel-metadata.tar"),
            "-C",
            str(root),
        )
        live("chroot", str(root), "/bin/sh", "/tmp/prepare-image.sh")
        audit = subprocess.check_output(
            ["chroot", str(root), "/usr/bin/dpkg", "--audit"], text=True
        )
        require(not audit.strip(), "Incomplete Debian package installation: " + audit)
    finally:
        live("umount", "--recursive", str(root / "dev"))
    for path in (
        "var/cache/apt/archives",
        "var/lib/apt/lists",
        "tmp",
        "root/.ssh",
        "dev",
    ):
        shutil.rmtree(root / path, ignore_errors=True)
    for path in ("dev", "proc", "sys", "run", "home", "var/log", "tmp", "var/tmp"):
        (root / path).mkdir(parents=True, exist_ok=True)
    (root / "var/log/fsck").mkdir(exist_ok=True)
    for path in (root / "var/log").rglob("*"):
        if path.is_file() and path.name != "config":
            path.unlink()
    (root / "tmp").chmod(0o1777)
    (root / "var/tmp").chmod(0o1777)
    for key in (root / "etc/ssh").glob("ssh_host_*"):
        key.unlink()
    (root / "etc/machine-id").write_text("")
    for path in (
        "var/lib/dbus/machine-id",
        "var/lib/systemd/random-seed",
        "var/lib/urandom/random-seed",
    ):
        (root / path).unlink(missing_ok=True)
    (root / "etc/fstab").write_text("/dev/sda1 / ext4 defaults 0 0\n")
    (root / "etc/hostname").write_text("localhost\n")
    (root / "etc/hosts").write_text("127.0.0.1 localhost\n")
    (root / "etc/default/locale").write_text("LANG=C.UTF-8\n")
    (root / "etc/apt/sources.list").write_text(
        "deb https://deb.debian.org/debian trixie main\n"
        "deb https://deb.debian.org/debian trixie-updates main\n"
        "deb https://security.debian.org/debian-security trixie-security main\n"
    )
    disk = destination / "root.img"
    filesystem = destination / "rootfs.img"
    disk_size = 20 * 1024**3
    filesystem_size = disk_size - 2 * 1024**2
    with disk.open("wb") as output:
        output.truncate(disk_size)
    live(
        "sgdisk",
        "--clear",
        f"--new=1:2048:+{filesystem_size // 1024**2}M",
        "--typecode=1:8300",
        "--change-name=1:rootfs",
        str(disk),
    )
    with filesystem.open("wb") as output:
        output.truncate(filesystem_size)
    print("Creating GPT/ext4 disk and Incus metadata", file=sys.stderr, flush=True)
    live("mkfs.ext4", "-q", "-L", "rootfs", "-d", str(root), str(filesystem))
    live(
        "dd",
        "if=" + str(filesystem),
        "of=" + str(disk),
        "bs=1M",
        "seek=1",
        "conv=notrunc,sparse",
        "status=none",
    )
    filesystem.unlink()
    live(
        "qemu-img",
        "convert",
        "-f",
        "raw",
        "-O",
        "qcow2",
        str(disk),
        str(destination / "disk.qcow2"),
    )
    disk.unlink()
    shutil.rmtree(root)
    shutil.copyfile(inputs / "kernel/bzImage", destination / "kernel")
    with (destination / "kernel").open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    metadata = {
        "architecture": "x86_64",
        "creation_date": int(time.time()),
        "properties": {
            "description": "Local Koi Debian 13 / runit / direct-boot q35",
            "os": "Debian",
            "release": "trixie",
            "koi.boot": "q35-runit-v3",
            "koi.kernel-sha256": digest,
            "koi.bundle": json.dumps(bundle, sort_keys=True, separators=(",", ":")),
        },
        "templates": {
            "/var/lib/koi/seed.json": {
                "when": ["create"],
                "template": "koi.json",
            },
        },
    }
    with tarfile.open(destination / "metadata.tar.gz", "w:gz") as archive:
        for path, content in (
            ("metadata.yaml", json.dumps(metadata)),
            (
                "templates/koi.json",
                '{{ config_get("user.koi.seed", "{}") }}',
            ),
        ):
            data = content.encode()
            entry = tarfile.TarInfo(path)
            entry.size = len(data)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(data))
    return digest


def build(inputs, pool):
    host = Path("/var/lib/koi/host.json")
    settings = json.loads(host.read_text())
    require(
        settings["pool"] == pool,
        "Image pool differs from the configured Koi pool",
    )
    with tempfile.TemporaryDirectory(prefix="koi-image-") as temporary:
        root = Path(temporary) / "image"
        digest = artifacts(inputs, root)
        kernel = Path("/usr/share/qemu/koi") / digest / "kernel"
        kernel.parent.mkdir(parents=True, exist_ok=True)
        if kernel.exists():
            require(
                kernel.read_bytes() == (root / "kernel").read_bytes(),
                "Installed kernel differs from its content address",
            )
        else:
            shutil.copyfile(root / "kernel", kernel)
            kernel.chmod(0o644)
        imported = run(
            "incus",
            "--force-local",
            "image",
            "import",
            str(root / "metadata.tar.gz"),
            str(root / "disk.qcow2"),
        )
        match = re.search(r"fingerprint: ([0-9a-f]{64})", imported.stdout)
        require(
            match is not None, "Missing imported image fingerprint: " + imported.stdout
        )
        fingerprint = match.group(1)
    settings = json.loads(host.read_text())
    image = validate_bundle(json.loads((Path(inputs) / "bundle.json").read_text()))[
        "image"
    ]
    settings["images"][image] = fingerprint
    atomic(host, json.dumps(settings, indent=2) + "\n")
    return {"fingerprint": fingerprint, "kernel": str(kernel)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", type=Path)
    parser.add_argument("pool", nargs="?")
    parser.add_argument(
        "--output", type=Path, help="Build artifacts without publishing to Incus"
    )
    args = parser.parse_args()
    if args.output is not None:
        require(args.pool is None, "Use either a pool or --output")
        digest = artifacts(args.inputs, args.output)
        print(json.dumps({"directory": str(args.output), "kernelSha256": digest}))
    else:
        require(args.pool is not None, "Specify an Incus pool or --output directory")
        print(json.dumps(build(args.inputs, args.pool)))


if __name__ == "__main__":
    try:
        main()
    except (Refused, OSError, ValueError, subprocess.CalledProcessError) as error:
        print("koi-build-image: " + str(error), file=sys.stderr)
        sys.exit(1)
