"""Assemble the supported Nix/runit image under fakeroot, without host privileges."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def run(*command):
    subprocess.run(command, check=True)


def install_userspace(root, inputs):
    payload = inputs / "guest-tools"
    manifest = json.loads((payload / "manifest.json").read_text())
    for path in (payload / "store-paths").read_text().splitlines():
        if not path.startswith("/nix/store/") or Path(path).parent != Path("/nix/store"):
            raise ValueError("Invalid declared guest store path: " + path)
        run("cp", "-a", "--reflink=auto", "--no-preserve=ownership", path, str(root / "nix/store"))
    (root / "opt/koi-tools").symlink_to(manifest["profile"])
    (root / "opt/koi-system").symlink_to(manifest["systemProfile"])
    for profile, alias in ((manifest["systemProfile"], "koi-system"),
                           (manifest["profile"], "koi-tools")):
        for directory in ("bin", "sbin"):
            binaries = root / profile.lstrip("/") / directory
            if not binaries.exists():
                continue
            for binary in binaries.iterdir():
                target = root / "usr" / directory / binary.name
                if target.is_symlink():
                    raise ValueError("Conflicting declared guest executable: " + binary.name)
                target.symlink_to("/opt/" + alias + "/" + directory + "/" + binary.name)
    if not (root / "usr/bin/sh").is_symlink():
        (root / "usr/bin/sh").symlink_to("bash")
    (root / "usr/sbin/init").symlink_to("/usr/bin/runit-init")
    sudo = root / "usr/bin/sudo"
    sudo.unlink()
    shutil.copyfile(root / manifest["sudo"].lstrip("/"), sudo)
    sudo.chmod(0o4755)
    for relative in ("usr/sbin/sudo", "usr/bin/sudoedit", "usr/sbin/sudoedit"):
        alias = root / relative
        alias.unlink(missing_ok=True)
        alias.symlink_to("/usr/bin/sudo")
    helper = root / "usr/local/libexec/unix_chkpwd"
    shutil.copyfile(root / manifest["pamHelper"].lstrip("/"), helper)
    helper.chmod(0o4755)
    (root / "nix/var/nix/gcroots/koi-system").symlink_to("/opt/koi-system")
    (root / "nix/var/nix/gcroots/koi-tools").symlink_to("/opt/koi-tools")
    (root / "nix/var/nix/gcroots/koi-test-shell").symlink_to(manifest["bash"])
    with (payload / "registration").open("rb") as registration:
        subprocess.run([manifest["nixStore"], "--store", "local?root=" + str(root),
                        "--option", "build-users-group", "", "--load-db"],
                       stdin=registration, check=True)
    for source in (payload / "home").rglob("*"):
        if source.is_file():
            target = root / "home/dev" / source.relative_to(payload / "home")
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(0o600)
    shutil.copyfile(payload / "manifest.json", root / "etc/koi-tools.json")
    return manifest


def package_environment():
    return {key: value for key, value in os.environ.items()
            if key not in ("LD_PRELOAD", "FAKEROOTKEY")}


def verify_tools(manifest):
    with tempfile.TemporaryDirectory(prefix="koi-tool-version-") as home:
        env = {**package_environment(), "HOME": home, "XDG_CONFIG_HOME": home + "/config",
               "XDG_CACHE_HOME": home + "/cache", "XDG_STATE_HOME": home + "/state"}
        for tool in manifest["versionPrograms"]:
            actual = subprocess.check_output([tool["binary"], "--version"], text=True, env=env).strip()
            if actual != tool["expected"]:
                raise ValueError("Guest tool version mismatch: " + actual)


def startup_manifest(root, filename, programs, ldd):
    paths = []
    for program in programs:
        paths.append(program)
        binary = program if program.startswith("/nix/store/") else str(root / program.lstrip("/"))
        result = subprocess.run([ldd, binary], text=True, capture_output=True,
                                env={**package_environment(), "LC_ALL": "C"})
        text = result.stdout + result.stderr
        if result.returncode and not any(message in text for message in
                                         ("not a dynamic executable", "statically linked")):
            result.check_returncode()
        if "not found" in text:
            raise ValueError("Missing startup dependency: " + program)
        for line in text.splitlines():
            match = re.search(r"(?:=>\s+|^\s*)(/\S+)", line)
            if match:
                dependency = match.group(1)
                if not (root / dependency.lstrip("/")).exists():
                    raise ValueError("Undeclared startup dependency: " + dependency)
                paths.append(dependency)
    (root / "etc/koi" / filename).write_text(
        "".join(path + "\n" for path in dict.fromkeys(paths)))


def configure_root(root):
    for relative in ("dev", "dev/pts", "dev/shm", "proc", "sys", "run", "tmp", "var/tmp",
                     "usr/bin", "usr/sbin", "usr/lib/modules", "usr/local/bin", "usr/local/libexec", "opt",
                     "etc/runit", "etc/koi", "etc/pam.d", "etc/security", "etc/ssh", "etc/nix",
                     "etc/sudoers.d", "etc/ssl/certs", "etc/profile.d", "etc/acpi/events", "etc/udev",
                     "etc/sysctl.d", "etc/service", "etc/sv", "home/dev", "root", "var/empty",
                     "var/lib/koi", "var/lib/chrony", "var/db/sudo", "var/log", "var/cache",
                     "nix/store", "nix/var/nix/gcroots"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    for relative in ("tmp", "var/tmp"):
        (root / relative).chmod(0o1777)
    for name, target in (("bin", "usr/bin"), ("sbin", "usr/sbin"), ("lib", "usr/lib")):
        (root / name).symlink_to(target)
    (root / "var/run").symlink_to("/run")
    (root / "var/lock").symlink_to("/run/lock")
    accounts = [
        ("root", 0, 0, "/root", "/bin/bash"),
        ("dev", 1000, 1000, "/home/dev", "/bin/bash"),
        ("sshd", 74, 74, "/var/empty", "/bin/nologin"),
        ("chrony", 75, 75, "/var/lib/chrony", "/bin/nologin"),
    ] + [("nixbld" + str(i), 30000 + i, 30000, "/var/empty", "/bin/nologin") for i in range(1, 11)]
    (root / "etc/passwd").write_text("".join(
        f"{name}:x:{uid}:{gid}:{name}:{home}:{shell}\n" for name, uid, gid, home, shell in accounts))
    (root / "etc/shadow").write_text("".join(f"{name}:*:1:0:99999:7:::\n" for name, *_ in accounts))
    (root / "etc/shadow").chmod(0o600)
    groups = [("root", 0, ""), ("tty", 5, ""), ("disk", 6, ""), ("dev", 1000, ""),
              ("sshd", 74, ""), ("chrony", 75, ""), ("docker", 76, ""),
              ("nixbld", 30000, ",".join("nixbld" + str(i) for i in range(1, 11)))]
    (root / "etc/group").write_text("".join(f"{name}:x:{gid}:{members}\n" for name, gid, members in groups))
    (root / "etc/nsswitch.conf").write_text("passwd: files\ngroup: files\nshadow: files\nhosts: files dns\n")
    (root / "etc/machine-id").write_text("")
    (root / "etc/fstab").write_text("/dev/vda1 / ext4 defaults 0 0\n")
    (root / "etc/hostname").write_text("localhost\n")
    (root / "etc/hosts").write_text("127.0.0.1 localhost\n")
    (root / "etc/shells").write_text("/bin/bash\n/bin/sh\n")
    (root / "etc/os-release").write_text('NAME="Koi"\nID=koi\n')
    (root / "etc/resolv.conf").write_text("")


def build(inputs, output):
    output.mkdir(mode=0o700)                                                    
    previous_umask = os.umask(0o022)
    try:
        assemble(inputs.resolve(), output)
    finally:
        os.umask(previous_umask)


def guest_ownership(root):
    if not os.environ.get("FAKEROOTKEY"):
        raise ValueError("Use the standalone builder's fakeroot wrapper")
    run("chown", "-hR", "0:0", str(root))
    run("chown", "-hR", "1000:1000", str(root / "home/dev"))
    run("chown", "75:75", str(root / "var/lib/chrony"))
    run("chown", "0:30000", str(root / "nix/store"))
    (root / "nix/store").chmod(0o1775)
    (root / "usr/bin/sudo").chmod(0o4755)
    (root / "usr/local/libexec/unix_chkpwd").chmod(0o4755)
    (root / "root").chmod(0o700)
    (root / "home/dev").chmod(0o700)


def assemble(inputs, output):
    root = output / "root"
    configure_root(root)
    manifest = install_userspace(root, inputs)
    for source, target in (
        ("stage1.sh", "etc/runit/1"),
        ("first-boot.sh", "usr/local/libexec/koi-first-boot"),
        ("ready", "usr/local/libexec/koi-ready"),
        ("prefetch", "usr/local/libexec/koi-prefetch"),
        ("busybox", "usr/local/libexec/busybox"),
        ("network.sh", "usr/local/libexec/koi-network"),
    ):
        shutil.copyfile(inputs / source, root / target)
        (root / target).chmod(0o755)
    shutil.copytree(inputs / "kernel/lib/modules", root / "usr/lib/modules", dirs_exist_ok=True,
                    symlinks=True)
    run("sh", str(inputs / "prepare-image.sh"), str(root), manifest["pamModules"],
        manifest["sshd"], manifest["udevd"], manifest["caBundle"])
    for service in (inputs / "guest-tools/services").iterdir():
        directory = root / "etc/sv" / ("koi-" + service.name)
        directory.mkdir()
        shutil.copyfile(service, directory / "run")
        (directory / "run").chmod(0o755)
        (directory / "supervise").symlink_to("/run/koi-supervise/koi-" + service.name)
        (root / "etc/service" / service.name).symlink_to("/etc/sv/koi-" + service.name)
    verify_tools(manifest)
    subprocess.run([manifest["sshd"], "-G", "-f", str(root / "etc/ssh/sshd_config")],
                   check=True, stdout=subprocess.DEVNULL, env=package_environment())
    modules = [manifest["pamModules"] + "/" + name + ".so" for name in
               ("pam_unix", "pam_nologin", "pam_limits", "pam_exec")]
    startup_manifest(root, "startup-files", manifest["entryPrograms"] + modules +
                     ["/usr/local/libexec/unix_chkpwd"], manifest["ldd"])
    startup_manifest(root, "service-files", manifest["servicePrograms"], manifest["ldd"])
    guest_ownership(root)
    disk = output / "root.img"
    filesystem = output / "rootfs.img"
    size = 20 * 1024**3
    filesystem_size = size - 2 * 1024**2
    with disk.open("wb") as stream:
        stream.truncate(size)
    run("sgdisk", "--clear", "--new=1:2048:+" + str(filesystem_size // 1024**2) + "M",
        "--typecode=1:8300", str(disk))
    with filesystem.open("wb") as stream:
        stream.truncate(filesystem_size)
    run("mkfs.ext4", "-q", "-L", "rootfs", "-d", str(root), str(filesystem))
    run("e2fsck", "-fn", str(filesystem))
    run("dd", "if=" + str(filesystem), "of=" + str(disk), "bs=1M", "seek=1",
        "conv=notrunc,sparse", "status=none")
    filesystem.unlink()
    run("chmod", "-R", "u+w", str(root))
    shutil.rmtree(root)
    shutil.copyfile(inputs / "kernel/vmlinux", output / "kernel")
    shutil.copyfile(inputs / "kernel/.config", output / "kernel.config")
    with (output / "kernel").open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    (output / "image.json").write_text(json.dumps({
        "format": "koi-1", "diskBytes": size, "kernelSha256": digest,
        "guest": "nix-runit", "rootDevice": "/dev/vda1", "bootTrace": 1, "sshCertificates": 1, "staticNetworking": 1,
        "inferenceRelay": 1,
        "tools": json.loads((inputs / "guest-tools/manifest.json").read_text())["versions"],
    }, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.inputs, args.output.resolve())
