"""Export standalone Koi configuration or prepare a reviewed NixOS installation.

Preparation is unprivileged. The operator explicitly runs --apply to preserve
the existing configuration and build the next boot, and --finish after reboot
to build the image unprivileged and invoke Koi's ordinary installation command.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shlex
import stat
import subprocess
import sys
import tempfile

CONFIGURATION = Path("/etc/nixos/configuration.nix")
BACKUP = CONFIGURATION.with_name("configuration.before-koi.nix")
MODULE = CONFIGURATION.with_name("koi-setup.nix")
RECEIPT = CONFIGURATION.with_name("koi-setup.json")


def nix_string(value):
    return json.dumps(str(value)).replace("${", r"\${")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def command(*argv, capture=False):
    result = subprocess.run(argv, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def settings_text(operator, host_id):
    return f'''{{ lib, ... }}:
{{
  boot.supportedFilesystems = [ "zfs" ];
  boot.zfs.forceImportRoot = lib.mkDefault false;
  networking.hostId = lib.mkDefault {nix_string(host_id)};
  services.koi = {{
    enable = true;
    operator = {nix_string(operator)};
  }};
}}
'''


def module_text(source, operator, host_id):
    return f'''{{ ... }}:
let koi = builtins.getFlake {nix_string("path:" + source)};
in {{
  imports = [ koi.nixosModules.default ({settings_text(operator, host_id).strip()}) ];
  # Retain the exact recipe used by this installation across store GC.
  system.extraDependencies = [ koi.outPath ];
}}
'''


def flake_text(source):
    return f'''{{
  description = "Standalone Koi configuration";
  inputs.koi.url = {nix_string("path:" + source)};
  outputs = {{ koi, ... }}: {{
    nixosModules.default = {{ ... }}: {{
      imports = [ koi.nixosModules.default ./settings.nix ];
      system.extraDependencies = [ koi.outPath ];
    }};
    packages.x86_64-linux = {{
      inherit (koi.packages.x86_64-linux) koi koi-user-tools image;
    }};
  }};
}}
'''


def output_directory(args):
    if args.output:
        directory = args.output.absolute()
        directory.mkdir(mode=0o700)
    else:
        parent = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "koi-setup"
        parent.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="plan-", dir=parent))
    return directory


def export_config(args, source):
    """Generate Koi-owned configuration without evaluating any host configuration."""
    operator = args.operator or pwd.getpwuid(os.getuid()).pw_name
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*", operator):
        raise ValueError("Choose a valid existing operator name on the target host")
    host_id = args.host_id or Path("/etc/machine-id").read_text().strip()[:8]
    if not re.fullmatch(r"[0-9a-f]{8}", host_id):
        raise ValueError("Choose an eight-digit hexadecimal --host-id for the target host")
    directory = output_directory(args)
    (directory / "settings.nix").write_text(settings_text(operator, host_id))
    (directory / "flake.nix").write_text(flake_text(source))
    command("nix", "flake", "lock", "path:" + str(directory))
    command("nix-store", "--add-root", str(directory / "source"), "--indirect", "--realise", source, capture=True)
    print(f"Standalone Koi flake: {directory}\n"
          "Only Koi settings and locked dependencies are included. No host configuration was read.\n"
          "Keep flake.nix, flake.lock and settings.nix together. To import from a non-flake host:\n\n"
          "  (builtins.getFlake " + nix_string("path:" + str(directory)) + ").nixosModules.default\n\n"
          "For a host flake, add this directory as an input and import its nixosModules.default.\n"
          "The target host must declare the operator and use mutable users and a ZFS-compatible kernel.\n"
          "After applying the host configuration, build/import the matching image:\n"
          + shlex.join(["nix", "build", "path:" + str(directory) + "#image", "--out-link", "koi-image"])
          + "\nkoi install --image ./koi-image\nkoi new project")


def wrapper_text():
    return '''# Prepared by koi-setup; original configuration is preserved alongside it.
{
  imports = [
    ./configuration.before-koi.nix
    ./koi-setup.nix
  ];
}
'''


def evaluate(configuration, operator):
    expression = f'''let
      host = import <nixpkgs/nixos> {{
        configuration = builtins.toPath {nix_string(configuration)};
      }};
      cfg = host.config;
    in {{
      kernel = cfg.boot.kernelPackages.kernel.version;
      hostId = cfg.networking.hostId;
      mutableUsers = cfg.users.mutableUsers;
      operatorExists = builtins.hasAttr {nix_string(operator)} cfg.users.users;
      koiEnabled = cfg.services.koi.enable or false;
      zfsBroken = if cfg.boot.zfs.enabled then cfg.boot.zfs.modulePackage.meta.broken or false else false;
      failedAssertions = map (a: a.message) (builtins.filter (a: !a.assertion) cfg.assertions);
      systemDerivation = cfg.system.build.toplevel.drvPath;
    }}'''
    return json.loads(command("nix-instantiate", "--eval", "--strict", "--json",
                              "--expr", expression, capture=True))


def check_report(report):
    errors = list(report["failedAssertions"])
    if report["zfsBroken"]:
        errors.append("The selected kernel's ZFS package is marked unsupported; choose a compatible kernel explicitly")
    if not report["operatorExists"]:
        errors.append("The operator must already be declared in the host configuration")
    if not report["mutableUsers"]:
        errors.append("Koi requires mutable users; review users.mutableUsers on this host")
    if errors:
        raise ValueError("Configuration checks failed:\n" + "\n".join(errors))


def prepare(args, source, entry):
    if os.geteuid() == 0:
        raise ValueError("Run preparation as your ordinary user")
    if RECEIPT.exists():
        print("Koi setup was already applied. After reboot, run:\n"
              + shlex.join([entry, "--finish"]))
        return
    if Path("/etc/nixos/flake.nix").exists():
        raise ValueError("This prototype supports configuration.nix hosts; flake hosts use INSTALL.md")
    if BACKUP.exists() or MODULE.exists():
        raise ValueError("Existing Koi setup files need inspection before preparing another installation")
    operator = args.operator or pwd.getpwuid(os.getuid()).pw_name
    pwd.getpwnam(operator)
    original = CONFIGURATION.read_bytes()
    original_report = evaluate(CONFIGURATION, operator)
    if original_report["koiEnabled"]:
        raise ValueError("Koi is already enabled; use the existing image/install workflow in INSTALL.md")
    host_id = original_report["hostId"] or Path("/etc/machine-id").read_text().strip()[:8]
    if len(host_id) != 8 or any(c not in "0123456789abcdef" for c in host_id):
        raise ValueError("The host needs a valid eight-digit hexadecimal networking.hostId")
    directory = output_directory(args)
    module = module_text(source, operator, host_id)
    (directory / "koi-setup.nix").write_text(module)
    (directory / "configuration.nix").write_text(wrapper_text())
    preview = directory / "preview.nix"
    preview.write_text("{ imports = [ (builtins.toPath " + nix_string(CONFIGURATION)
                       + ") ./koi-setup.nix ]; }\n")
    print("Checking the merged NixOS configuration…", flush=True)
    report = evaluate(preview, operator)
    check_report(report)
    plan = {"source": source, "operator": operator, "hostId": host_id,
            "originalSha256": digest(original), "report": report}
    (directory / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    for name, argv in (("apply", [entry, "--apply", str(directory)]),
                       ("finish", [entry, "--finish"])):
        script = directory / name
        script.write_text("#!/bin/sh\nexec " + shlex.join(argv) + ' "$@"\n')
        script.chmod(0o700)
    command("nix-store", "--add-root", str(directory / "source"), "--indirect", "--realise", source, capture=True)
    command("nix-store", "--add-root", str(directory / "tool"), "--indirect",
            "--realise", str(Path(entry).parent.parent), capture=True)
    print(f"\nPrepared: {directory}\n"
          f"Operator: {operator}\n"
          f"Kernel: {report['kernel']} (existing host selection retained)\n"
          f"ZFS host ID: {report['hostId']}\n"
          "Storage: 32 GiB sparse pool capacity; physical usage grows with writes.\n"
          "Your existing configuration will be preserved as " + str(BACKUP) + ".\n\n"
          "Review the two .nix files above. To apply and build the next boot:\n"
          + shlex.join(["sudo", str(directory / "apply")])
          + "\n\nThen reboot and finish:\n"
          "sudo reboot\n" + shlex.join([str(directory / "finish")]))


def checked_plan(directory, source):
    plan = json.loads((directory / "plan.json").read_text())
    if plan["source"] != source:
        raise ValueError("Run the exact setup executable printed by preparation")
    expected = module_text(source, plan["operator"], plan["hostId"])
    if (directory / "koi-setup.nix").read_text() != expected:
        raise ValueError("Prepared module changed; prepare a new plan")
    if (directory / "configuration.nix").read_text() != wrapper_text():
        raise ValueError("Prepared configuration changed; prepare a new plan")
    check_report(plan["report"])
    return plan, expected


def root_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError("Expected a root-owned, non-writable regular file: " + str(path))
    return path.read_bytes()


def atomic_file(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=".koi-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def apply(directory, source):
    if os.geteuid() != 0:
        raise ValueError("The operator must run --apply with sudo")
    info = CONFIGURATION.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError("/etc/nixos must be a root-owned, non-writable directory")
    plan, module = checked_plan(directory, source)
    current = root_file(CONFIGURATION)
    wrapper = wrapper_text().encode()
    if BACKUP.exists():
        if digest(root_file(BACKUP)) != plan["originalSha256"] or current not in (wrapper, root_file(BACKUP)):
            raise ValueError("Existing configuration or backup differs from the prepared plan")
    else:
        if digest(current) != plan["originalSha256"]:
            raise ValueError("Host configuration changed since preparation; prepare a new plan")
    for path, expected in ((MODULE, module.encode()), (RECEIPT, (json.dumps(plan, indent=2) + "\n").encode())):
        if path.exists() and root_file(path) != expected:
            raise ValueError("Existing setup file differs from the prepared plan: " + str(path))
    report = evaluate(directory / "preview.nix", plan["operator"])
    check_report(report)
    if current != wrapper and report != plan["report"]:
        raise ValueError("Merged host configuration changed since preparation; prepare a new plan")
    if not BACKUP.exists():
        with BACKUP.open("xb") as backup:
            os.chmod(BACKUP, 0o644)
            backup.write(current)
            backup.flush()
            os.fsync(backup.fileno())
    atomic_file(MODULE, module.encode())
    atomic_file(RECEIPT, (json.dumps(plan, indent=2) + "\n").encode())
    atomic_file(CONFIGURATION, wrapper)
    print("Building the next boot. The running system changes after reboot.", flush=True)
    command("/run/current-system/sw/bin/nixos-rebuild", "boot")
    print("Next boot is prepared. Run sudo reboot, then koi-setup --finish.")


def finish():
    if os.geteuid() == 0:
        raise ValueError("Run --finish as the ordinary operator; the image build is unprivileged")
    plan = json.loads(root_file(RECEIPT))
    source = plan["source"]
    if not source.startswith("/nix/store/") or not Path(source).is_dir():
        raise ValueError("The installed image recipe is unavailable")
    if root_file(MODULE).decode() != module_text(source, plan["operator"], plan["hostId"]):
        raise ValueError("The installed setup module differs from its receipt")
    if not Path("/etc/koi/settings.json").exists() or not platform.release().startswith(plan["report"]["kernel"]):
        raise ValueError("Boot the prepared NixOS generation before running --finish")
    if pwd.getpwuid(os.getuid()).pw_name != plan["operator"]:
        raise ValueError("Run --finish as the configured operator: " + plan["operator"])
    print("Building or fetching the image from this installation's exact recipe…", flush=True)
    image = command("nix", "build", "path:" + source + "#image", "--no-link",
                    "--print-out-paths", capture=True)
    if len(image.splitlines()) != 1:
        raise ValueError("Expected one finished image directory")
    command("/run/current-system/sw/bin/koi", "install", "--image", image)
    print("Ready. Run: koi new project")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument("--apply", type=Path, metavar="PLAN", help="Operator-only: preserve config and build next boot")
    operations.add_argument("--finish", action="store_true", help="After reboot: build image and install Koi state")
    operations.add_argument("--export-config", action="store_true", help="Write a standalone locked Koi flake without reading the host configuration")
    parser.add_argument("--operator", help="Existing local user; defaults to the invoking user")
    parser.add_argument("--output", type=Path, help="New directory for review files")
    parser.add_argument("--host-id", help="Target host's eight-digit ZFS host ID; used by --export-config")
    args = parser.parse_args()
    if args.host_id and not args.export_config:
        parser.error("--host-id is used with --export-config")
    source = os.environ["KOI_SETUP_SOURCE"]
    entry = str(Path(os.environ["KOI_SETUP_ENTRY"]).resolve())
    try:
        if args.export_config:
            export_config(args, source)
            return
        if not Path("/etc/NIXOS").exists() or platform.machine() != "x86_64" or not Path("/dev/kvm").exists():
            raise ValueError("Setup requires an x86-64 NixOS host with KVM")
        if args.apply:
            apply(args.apply.absolute(), source)
        elif args.finish:
            finish()
        else:
            prepare(args, source, entry)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, "koi-setup: " + str(error) + "\n")


if __name__ == "__main__":
    main()
