"""Fixed systemd confinement for direct Cloud Hypervisor processes."""

import argparse
from pathlib import Path

import trial


def quoted(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def bind_spec(value):
    return ":".join(quoted(part) for part in value.split(":"))


def service(vmm, closure, kernel, seed, disk, runtime, jail, account, tap, quiet=False, mac="02:00:00:00:00:03", cid=3, dependencies=(), trace=False, trace_launcher=None, inference_directory=None):
    binds = list(closure) + [str(kernel) + ":/kernel", str(seed) + ":/seed.img"]
    if inference_directory is not None:
        binds.append(str(inference_directory) + ':/inference')
    argv = trial.command(argparse.Namespace(kernel=Path("/kernel"), disk=Path("/dev/koi-disk"),
                                           seed=Path("/seed.img"), tap=tap, runtime=Path("/runtime"), mac=mac, cid=cid))
    argv[0] = str(vmm)
    if quiet:
        argv[argv.index("--cmdline") + 1] += " quiet"
    if trace:
        if trace_launcher is None:
            raise ValueError('Tracing requires the pinned exec marker')
        binds.append(str(Path(trace_launcher).parent.parent))
        argv.insert(0, str(trace_launcher))
        argv[argv.index("--cmdline") + 1] += " koi.trace=1"
        argv.extend(["--event-monitor", "path=/runtime/vmm-events.json"])
    argv[argv.index("--serial") + 1] = "file=/runtime/serial.log"
    settings = ["User=" + account, "Group=" + account, "RootDirectory=" + str(jail),
                "WorkingDirectory=/runtime", "NoNewPrivileges=yes", "CapabilityBoundingSet=",
                "PrivateDevices=yes", "DevicePolicy=closed", "PrivateTmp=yes",
                "ProtectSystem=strict", "ProtectHome=yes", "ProtectKernelTunables=yes",
                "ProtectKernelModules=yes", "ProtectKernelLogs=yes", "ProtectControlGroups=yes",
                "ProtectProc=invisible", "ProcSubset=pid", "RestrictSUIDSGID=yes",
                "LockPersonality=yes", "RestrictRealtime=yes", "SystemCallArchitectures=native",
                "BindReadOnlyPaths=" + " ".join(bind_spec(path) for path in binds),
                "BindPaths=" + " ".join(bind_spec(path) for path in
                    [str(disk) + ":/dev/koi-disk", str(runtime) + ":/runtime",
                     "/dev/kvm", "/dev/net/tun"]),
                "DeviceAllow=/dev/kvm rw", "DeviceAllow=/dev/net/tun rw",
                "DeviceAllow=" + str(disk) + " rw", "ReadWritePaths=/runtime",
                "ExecStart=" + " ".join(quoted(arg) for arg in argv)]
    units = " ".join(dependencies)
    ordering = "Requires=" + units + "\nAfter=" + units + "\n" if units else ""
    return "[Unit]\nDescription=Koi project machine\n" + ordering + "[Service]\n" + "\n".join(settings) + "\n"
