#!/usr/bin/env python3
"""Start, attach to and cleanly stop persistent project VMs.

One installation prepares owned host resources; the fixed sudo helper exposes
only project-machine operations, with no caller-selected host paths.
"""

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import channels
from disks import Disks, identifier, save, sync_directory
import isolation
import trial

UNITS = Path("/run/systemd/system")


def output(*argv):
    return subprocess.check_output(argv, text=True, timeout=35).strip()


def observed(unit):
    text = output("systemctl", "show", unit, "--property=ActiveState,MainPID")
    values = dict(line.split("=", 1) for line in text.splitlines())
    return values["ActiveState"], int(values["MainPID"])


def stopped(unit):
    state, pid = observed(unit)
    return state in ("inactive", "failed") and pid == 0


class Runtime:
    def __init__(self, state, trace=None):
        self.trace = trace
        self.store = Disks(state)
        self.root = self.store.state / "runtime"
        self.root.mkdir(mode=0o700, exist_ok=True)

    def machine(self, name, *, verify_kernel=True):
        identifier(name, r"[a-z][a-z0-9-]{0,30}")
        record = json.loads((self.store.state / "machines" / (name + ".json")).read_text())
        identifier(record["id"], r"[0-9a-f]{32}")
        if record["name"] != name or record["pending"] is not None:
            raise ValueError("Machine disk preparation is incomplete")
        generation = identifier(record["image"], r"[0-9a-f]{64}")
        base = self.store.dataset + "/images/" + generation
        volume = self.store.dataset + "/machines/" + record["id"]
        properties = self.store.properties(base, base + "@base", volume)
        image = self.store.image(generation, properties, verify_kernel=verify_kernel)
        values = self.store.owned(volume, "volume", base + "@base", properties)
        if values["readonly"][0] != "off" or int(values["volsize"][0]) != image["diskBytes"]:
            raise ValueError("Retained machine disk has changed")
        return record

    def directory(self, record):
        return self.root / record["id"]

    def configuration(self, record):
        directory = self.directory(record)
        config = json.loads((directory / "config.json").read_text())
        if (config["id"], config["name"], config["image"]) != (record["id"], record["name"], record["image"]):
            raise ValueError("Runtime binding differs from retained disk identity")
        return config

    @staticmethod
    def unit(record):
        return "koi-vm-" + record["id"] + ".service"

    def configure(self, args):
        with self.store.locked():
            record = self.machine(args.name)
            if not stopped(self.unit(record)):
                raise ValueError("Stop the machine before configuring it")
            directory = self.directory(record)
            if directory.exists():
                raise ValueError("Runtime is already configured; retained binding was not replaced")
            work = args.work.resolve()
            address = str(ipaddress.IPv4Address(args.address))
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,15}", args.tap):
                raise ValueError("Invalid prepared TAP name")
            account = pwd.getpwnam(args.account)
            if account.pw_uid == 0 or account.pw_gid == 0:
                raise ValueError("VMM account must be unprivileged")
            seed = json.loads(output("debugfs", "-R", "cat /config.json", str(work / "seed.img")))
            seed = trial.guest.validate(seed)
            public_key = " ".join(output("ssh-keygen", "-y", "-f", str(work / "management-key")).split()[:2])
            if (seed["id"], seed["name"], seed["ssh_key"]) != (record["id"], record["name"], public_key):
                raise ValueError("Prepared seed/key does not match this retained machine")
            fields = (work / "known_hosts").read_text().strip().split()
            if len(fields) != 3 or fields[0] != address or fields[1] != "ssh-ed25519":
                raise ValueError("Supply the fixture's pinned SSH host key at this address")
            trial.guest.validate({"id": record["id"], "name": record["name"], "ssh_key": " ".join(fields[1:])})
            config = {"id": record["id"], "name": record["name"], "image": record["image"],
                      "address": address, "hostKey": " ".join(fields[1:]),
                      "account": args.account, "tap": args.tap, "boot": None,
                      "pending": None}
            vmm = Path(shutil.which("cloud-hypervisor")).resolve()
            config["vmm"] = str(vmm)
            config["closure"] = output("nix-store", "-qR", str(vmm.parent.parent)).splitlines()
            directory.mkdir(mode=0o700)
            (directory / "boots").mkdir(mode=0o700)
            (directory / "jail").mkdir(mode=0o755)
            (directory / "jail").chmod(0o755)
            for filename in ("seed.img", "management-key", "known_hosts"):
                shutil.copyfile(work / filename, directory / filename)
                (directory / filename).chmod(0o600)
            (directory / "seed.img").chmod(0o444)
            (self.store.state / "kernels" / record["image"]).chmod(0o444)
            save(directory / "config.json", config)

    def args(self, record, config):
        if config.get('auth', 'public-key') not in ('public-key', 'certificate-v1'):
            raise ValueError('Unknown retained SSH authentication policy')
        connection = SimpleNamespace(work=self.directory(record), address=config["address"])
        if config.get('auth') == 'certificate-v1':
            import access
            connection.key = access.paths(self.store.state)['client']
            connection.certificate = self.directory(record) / 'management-cert.pub'
        return connection

    def disk(self, record):
        disk = (Path("/dev/zvol") / self.store.dataset / "machines" / record["id"]).resolve()
        if not stat.S_ISBLK(disk.stat().st_mode):
            raise ValueError("Retained project disk is not a block device")
        return disk

    def unit_text(self, record, config):
        import inference
        directory = self.directory(record)
        disk = self.disk(record)
        identifier(config["boot"], r"[0-9a-f]{16,32}")
        runtime = directory / "current"
        return isolation.service(config["vmm"], config["closure"],
                                 self.store.state / "kernels" / record["image"],
                                 directory / "seed.img", disk, runtime, directory / "jail",
                                 config["account"], config["tap"], quiet=config.get("quiet", False),
                                 mac=config.get("mac", "02:00:00:00:00:03"), cid=config.get("cid", 3),
                                 dependencies=tuple(config[key] for key in ("networkUnit", "dnsUnit") if key in config),
                                 trace=config.get("trace", False),
                                 trace_launcher=Path(shutil.which('koi-trace-exec')).resolve() if config.get('trace', False) else None,
                                 inference_directory=inference.socket_directory(record) if config.get('inferenceRelay') == 1 else None)

    def inference(self, record, config, action):
        if config.get('inferenceRelay') == 1:
            import host
            import inference
            installation = host.load(self.store.state)
            if action == 'start':
                inference.ensure(installation, record, config)
            else:
                inference.stop(installation, record)

    def check_unit(self, record, config, live=False):
        path = UNITS / self.unit(record)
        if config["boot"] is None or not path.is_file():
            raise ValueError("Running unit differs from the recorded runtime; refusing to take control")
        text = path.read_text()
        if (hashlib.sha256(text.encode()).hexdigest() != config.get("unitHash")
                and text != self.unit_text(record, config)):
            raise ValueError("Running unit differs from the recorded runtime; refusing to take control")
        if live and config.get("stableRuntime"):
            directory = self.directory(record)
            current = directory / "current"
            expected = directory / "boots" / identifier(config["boot"], r"[0-9a-f]{16,32}")
            if not current.is_symlink() or current.resolve() != expected:
                raise ValueError("Runtime binding differs from the running boot; refusing to take control")

    def wait_ssh(self, record, config, timeout=30, connection=None):
        deadline = time.monotonic() + timeout
        while True:
            result = channels.ssh(connection or self.args(record, config), "true", check=False)
            if result.returncode == 0:
                return
            if result.returncode != 255:
                raise RuntimeError("Guest readiness command failed: " + result.stderr.strip())
            if any(value in result.stderr for value in ("HOST IDENTIFICATION HAS CHANGED", "Host key verification failed", "unix_listener:")):
                raise RuntimeError(result.stderr.strip())
            state, pid = observed(self.unit(record))
            if state not in ("active", "activating") or pid == 0 or time.monotonic() >= deadline:
                raise RuntimeError("Guest did not become reachable; retained logs: " +
                                   str(self.directory(record) / "boots" / config["boot"]) + "\n" + result.stderr.strip())
            time.sleep(0.05)                                                                   

    def start(self, name, timings=False, verbose_boot=False, *, multiplex=False):
        began = time.monotonic()
        if self.trace is not None:
            self.trace.mark('validation.begin')
        with self.store.locked():
            return self._start(name, timings, verbose_boot, multiplex=multiplex, began=began)

    def _start(self, name, timings=False, verbose_boot=False, *, multiplex=False, record=None, began=None):
        """Caller holds the storage lock; provision may supply its validated record."""
        if began is None:
            began = time.monotonic()
            if self.trace is not None:
                self.trace.mark('validation.begin')
        record = self.machine(name) if record is None else record
        config = self.configuration(record)
        connection = self.args(record, config)
        if multiplex:
            connection.control = self.directory(record) / ("a-" + uuid.uuid4().hex[:16])
            if len(os.fsencode(connection.control)) + 18 >= 108:
                raise ValueError("Runtime state path is too long for SSH connection sharing")
        unit = self.unit(record)
        state, pid = observed(unit)
        if config.get('auth') == 'certificate-v1':
            if config['pending'] == 'stop' or state in ('activating', 'deactivating'):
                raise RuntimeError('Machine has a live transition; certificate was not issued')
            import access
            if self.trace is not None:
                self.trace.mark('ssh.certificate.begin')
            access.issue(self.store.state, record['id'], connection.certificate)
            if self.trace is not None:
                self.trace.mark('ssh.certificate.end')
        phases = {"validation": time.monotonic() - began}
        if self.trace is not None:
            self.trace.mark('validation.end')
            image = json.loads((self.store.state / 'images' / (record['image'] + '.json')).read_text())
            if state == 'active' or image.get('bootTrace') != 1:
                raise ValueError('Tracing requires a stopped VM with the bootTrace=1 image')
        if state == "active" and pid:
            self.check_unit(record, config, live=True)
            if config["pending"] == "stop":
                raise RuntimeError("A clean stop is pending; finish koi stop before restarting")
            self.inference(record, config, 'start')
            self.wait_ssh(record, config, connection=connection)
            config["pending"] = None
            save(self.directory(record) / "config.json", config)
            message = name + " already running"
        elif state in ("activating", "deactivating") or pid:
            raise RuntimeError("Machine has a live transition; retry after inspecting " + unit)
        elif state in ("inactive", "failed"):
            for path in self.root.glob("*/config.json"):
                other = json.loads(path.read_text())
                if other["id"] != record["id"] and (other["tap"] == config["tap"] or other["account"] == config["account"]):
                    if not stopped(self.unit(other)):
                        raise ValueError("Another running machine owns this TAP or VMM account")
            directory = self.directory(record)
            unit_path = UNITS / unit
            unit_existed = unit_path.exists()
            if unit_existed:
                self.check_unit(record, config)
            config["boot"] = uuid.uuid4().hex[:16]
            if len(os.fsencode(directory / "boots" / config["boot"] / "vsock_1024")) >= 108:
                raise ValueError("Runtime state path is too long for Unix sockets")
            config["pending"] = "start"
            config["quiet"] = not verbose_boot
            config['trace'] = self.trace is not None
            previous_hash = config.get("unitHash")
            previous_stable = config.get("stableRuntime", False)
            save(directory / "config.json", config)                                           
            runtime = directory / "boots" / config["boot"]
            runtime.mkdir(mode=0o700)
            if self.trace is not None:
                self.trace.bind(directory / 'start-trace.json', record, config['boot'])
            account = pwd.getpwnam(config["account"])
            os.chown(runtime, account.pw_uid, account.pw_gid)
            if config.get('inferenceRelay') == 1:
                (runtime / 'vsock_8400').symlink_to('/inference/relay.sock')
                self.inference(record, config, 'start')
            disk = self.disk(record)
            os.chown(disk, account.pw_uid, account.pw_gid)
            disk.chmod(0o600)
            (directory / "seed.img").chmod(0o444)
            (self.store.state / "kernels" / record["image"]).chmod(0o444)
            temporary = directory / ("current-" + config["boot"])
            temporary.symlink_to(runtime)
            os.replace(temporary, directory / "current")
            sync_directory(directory)
            config["stableRuntime"] = True
            text = self.unit_text(record, config)
            changed = not unit_path.exists() or unit_path.read_text() != text
            if changed:
                if unit_path.exists():
                    unit_path.write_text(text)
                else:
                    with unit_path.open("x") as stream:
                        stream.write(text)
            (runtime / "unit.service").write_text(text)
            config["unitHash"] = hashlib.sha256(text.encode()).hexdigest()
            if not previous_stable or config["unitHash"] != previous_hash:
                save(directory / "config.json", config)
            if changed and unit_existed:
                if self.trace is not None:
                    self.trace.mark('host.daemon-reload.begin')
                output("systemctl", "daemon-reload")
                if self.trace is not None:
                    self.trace.mark('host.daemon-reload.end')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                path = runtime / "vsock_1024"
                listener.bind(str(path))
                os.chown(path, account.pw_uid, account.pw_gid)
                listener.listen(1)
                launched = time.monotonic()
                phases["preparation"] = launched - began - phases["validation"]
                if self.trace is not None:
                    self.trace.watch_vmm(runtime / 'vmm-events.json')
                    self.trace.mark('launch.request')
                output("systemctl", "start", unit)
                if self.trace is not None:
                    self.trace.mark('launch.command-returned')
                receive_options = {'trace': self.trace} if self.trace is not None else {}
                host_key = channels.receive(listener, record, config["hostKey"], address=config["address"], **receive_options)
                if config["hostKey"] is None:
                    config["hostKey"] = host_key
                    from disks import atomic
                    atomic(directory / "known_hosts", (config["address"] + " " + host_key + "\n").encode())
                    save(directory / "config.json", config)
                phases["launchToReady"] = time.monotonic() - launched
                ready = time.monotonic()
                if self.trace is not None:
                    self.trace.mark('ssh.begin')
                channels.ssh(connection, "true")
                if self.trace is not None:
                    self.trace.mark('ssh.end')
                phases["ssh"] = time.monotonic() - ready
            config["pending"] = None
            save(directory / "config.json", config)
            message = name + " ready"
        else:
            raise ValueError("Unexpected service state: " + state)
        phases["total"] = time.monotonic() - began
        print(message, flush=True)
        if timings:
            print(" ".join(key + "=" + f"{value:.3f}s" for key, value in phases.items()), file=sys.stderr)
        return connection

    def begin_stop(self, name):
        """Record and request shutdown; caller serializes host mutations."""
        with self.store.locked():
            record = self.machine(name, verify_kernel=False)
            config = self.configuration(record)
            unit = self.unit(record)
            if stopped(unit):
                if config["pending"] == "stop":
                    self.check_unit(record, config)
                    if output("systemctl", "show", unit, "--property=ExecMainStatus", "--value") != "0":
                        raise RuntimeError("Previous stop exited unsuccessfully; disk and logs retained")
                    config["pending"] = None
                    save(self.directory(record) / "config.json", config)
                self.inference(record, config, 'stop')
                print(name + " already stopped")
                return None
            self.check_unit(record, config, live=True)
            config["pending"] = "stop"
            save(self.directory(record) / "config.json", config)
            channels.request_poweroff(self.directory(record) / "boots" / config["boot"])
            return {'id': record['id'], 'name': name, 'boot': config['boot']}

    def wait_stop(self, ticket):
        """Wait without mutating ownership or holding a controller lock."""
        deadline = time.monotonic() + 30
        while not stopped(self.unit(ticket)):
            if time.monotonic() >= deadline:
                raise TimeoutError("Guest did not stop; disk and live VMM retained")
            time.sleep(0.05)

    def finish_stop(self, ticket):
        """Revalidate the exact boot after the unlocked shutdown wait."""
        with self.store.locked():
            record = self.machine(ticket['name'], verify_kernel=False)
            config = self.configuration(record)
            if (record['id'], config['boot'], config['pending']) != (ticket['id'], ticket['boot'], 'stop'):
                raise ValueError('Machine changed during shutdown; refusing cleanup')
            self.check_unit(record, config)
            unit = self.unit(record)
            if not stopped(unit):
                raise RuntimeError('Guest is still running; disk and live VMM retained')
            if output("systemctl", "show", unit, "--property=ExecMainStatus", "--value") != "0":
                raise RuntimeError("VMM exited unsuccessfully; disk and logs retained")
            self.inference(record, config, 'stop')
            config["pending"] = None
            save(self.directory(record) / "config.json", config)
        print(ticket['name'] + " stopped")

    def stop(self, name):
        ticket = self.begin_stop(name)
        if ticket is not None:
            self.wait_stop(ticket)
            self.finish_stop(ticket)

    def attach(self, name, command):
        connection = self.start(name, multiplex=True)
        self._attach(name, command, connection)

    def _attach(self, name, command, connection):
        script = 'mkdir -p "$HOME/projects/' + name + '" && cd "$HOME/projects/' + name + '" && '
        script += "exec " + shlex.join(command) if command else "exec bash -l"
        argv = channels.ssh_command(connection, script)
        if not command:
            argv.insert(1, "-t")
        if self.trace is not None:
            self.trace.mark('attach.exec')
            self.trace.finish()
        os.execvp(argv[0], argv)

    def ls(self):
        with self.store.locked():
            for path in sorted((self.store.state / "machines").glob("*.json")):
                record = json.loads(path.read_text())
                identifier(record["id"], r"[0-9a-f]{32}")
                identifier(record["name"], r"[a-z][a-z0-9-]{0,30}")
                if (self.directory(record) / "config.json").exists():
                    config = self.configuration(record)
                    state, _ = observed(self.unit(record))
                    print(record["name"], state, config["address"], config["pending"] or "-")
                else:
                    print(record["name"], "unconfigured", "-", record["pending"] or "-")


def sudo_executable():
    for candidate in ('/run/wrappers/bin/sudo', '/usr/bin/sudo', '/bin/sudo'):
        path = Path(candidate)
        if path.exists():
            info = path.stat()
            if info.st_uid == 0 and info.st_mode & stat.S_ISUID:
                return str(path)
    raise ValueError('Host has no root-owned setuid sudo command')


def helper_arguments(argv):
    if argv[:1] == ['import-login']:
        return len(argv) == 2 and argv[1] in ('codex', 'claude')
    if not argv or argv[0] not in ('new', 'start', 'stop', 'attach', 'ls', 'rm', 'bench', 'grant', 'revoke'):
        return False
    return not any(value == '--state' or value.startswith('--state=') or value == '--managed' for value in argv)


def main():
    entered = time.monotonic_ns()
    parser = argparse.ArgumentParser(prog="koi", description=__doc__)
    parser.add_argument("--version", action="version",
                        version="koi " + Path(__file__).with_name("VERSION").read_text().strip())
    if sys.argv[1:] == ["--version"]:
        parser.parse_args()
    import host
    managed_helper = sys.argv[1:2] == ['--managed']
    if not managed_helper and sys.argv[1:2] == ['login']:
        login_parser = argparse.ArgumentParser(prog='koi login', description='Log in on the host for VM inference')
        login_parser.add_argument('adapter', choices=('codex', 'claude'))
        login_parser.add_argument('login_options', nargs=argparse.REMAINDER,
                                  help='Options passed to the provider login command, e.g. --device-auth')
        login_args = login_parser.parse_args(sys.argv[2:])
        login_options = login_args.login_options
        if login_options[:1] == ['--']:
            login_options = login_options[1:]
        import inference
        try:
            inference.login(login_args.adapter, login_options)
            if os.geteuid() == 0:
                with host.operation_lock():
                    inference.import_login(host.load(), login_args.adapter, root_login=True)
                return
            sudo = sudo_executable()
            os.execv(sudo, [sudo, '-n', '--', '/etc/koi/bin/koi-root', 'import-login', login_args.adapter])
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
            parser.exit(1, 'koi login failed; check the host login and installation\n')
    if managed_helper:
        if os.geteuid() != 0:
            parser.error('The managed helper must run through its installed authorization')
        if not host.BINDING.exists():
            parser.error('No managed installation; the helper cannot access legacy fixtures')
        del sys.argv[1]
        if not helper_arguments(sys.argv[1:]):
            parser.error('The installed helper accepts only fixed VM operations')
    elif os.geteuid() != 0 and sys.argv[1:] not in (["--help"], ["-h"]):
        entry = os.environ.get('KOI_EXECUTABLE')
        if sys.argv[1:2] in (['install'], ['uninstall'], ['image']):
            if not entry:
                parser.error('Use the Nix-packaged command for setup')
            sudo = sudo_executable()
            os.execv(sudo, [sudo, '--', entry, *sys.argv[1:]])
        if not helper_arguments(sys.argv[1:]):
            parser.error('Use install once, then ordinary VM commands')
        sudo = sudo_executable()
        os.execv(sudo, [sudo, '-n', '--', '/etc/koi/bin/koi-root', *sys.argv[1:]])
    binding = Path("/etc/koi/state")
    state = Path(binding.read_text().strip()) if os.geteuid() == 0 and binding.exists() else Path("/var/lib/koi")
    if os.geteuid() == 0 and host.BINDING.exists():
        state = Path(host.private(host.BINDING)['state'])
    parser.add_argument("--state", type=Path, default=state)
    commands = parser.add_subparsers(dest="operation", required=True)
    for name in ("start", "stop", "attach"):
        sub = commands.add_parser(name)
        sub.add_argument("name")
        if name == "start":
            sub.add_argument("--timings", action="store_true")
            sub.add_argument("--verbose-boot", action="store_true", help="Keep informational kernel messages on the serial console")
        if name == "attach":
            sub.add_argument('--trace', action='store_true', help='Collect opt-in boot diagnostics')
            sub.add_argument("command", nargs=argparse.REMAINDER)
        if name == 'start':
            sub.add_argument('--trace', action='store_true', help='Collect opt-in boot diagnostics')
    commands.add_parser("ls")
    bench_parser = commands.add_parser('bench', help='Time the actual guest prompt in disposable VMs')
    bench_parser.add_argument('mode', nargs='?', choices=('fresh', 'retained'), default='fresh')
    bench_parser.add_argument('--samples', type=int, choices=range(1, 11), default=3)
    bench_parser.add_argument('--trace', action='store_true', help='Break down each entry across host, VMM and guest clocks')
    new_parser = commands.add_parser('new', help='Create or resume a persistent project VM')
    new_parser.add_argument('name')
    new_parser.add_argument('--no-attach', action='store_true')
    new_parser.add_argument('--trace', action='store_true', help='Collect opt-in boot diagnostics')
    remove_parser = commands.add_parser('rm', help='Delete an owned project disk')
    remove_parser.add_argument('name')
    remove_parser.add_argument('--yes', action='store_true')
    for name in ('grant', 'revoke'):
        sub = commands.add_parser(name, help=name.capitalize() + ' VM inference access')
        sub.add_argument('name')
    import_parser = commands.add_parser('import-login', help=argparse.SUPPRESS)
    import_parser.add_argument('adapter', choices=('codex', 'claude'))
    host.parser(commands)
    commands.add_parser('network-up', help=argparse.SUPPRESS)
    configure = commands.add_parser("configure", help="Bind an owned retained disk to explicitly prepared host resources")
    configure.add_argument("name")
    configure.add_argument("--work", type=Path, required=True, help="Successful fixture's seed, management key and pinned known_hosts")
    configure.add_argument("--tap", required=True)
    configure.add_argument("--address", required=True)
    configure.add_argument("--account", default="koi-trial")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("This first host CLI is root-operated; passwordless operator setup is not installed")
    os.umask(0o077)
    args.startup_trace = None
    if args.operation in ('new', 'start', 'attach') and args.trace:
        from startup_trace import StartupTrace
        args.startup_trace = StartupTrace(entered)
    try:
        if args.startup_trace is not None:
            args.startup_trace.mark('host.operation-lock.begin')
        with host.operation_lock() if args.operation not in ('network-up', 'bench') else __import__('contextlib').nullcontext():
            if args.startup_trace is not None:
                args.startup_trace.mark('host.operation-lock.end')
            dispatch(args)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, "koi: " + str(error) + "\n")
    finally:
        if args.startup_trace is not None:
            original = sys.exception()
            try:
                args.startup_trace.finish()
            except (OSError, ValueError) as error:
                if original is None:
                    raise
                print('Trace persistence also failed: ' + str(error), file=sys.stderr)


def dispatch(args):
    import host
    if args.operation in ('grant', 'revoke', 'import-login'):
        import inference
        config = host.load()
        if args.operation == 'import-login':
            inference.import_login(config, args.adapter)
        else:
            inference.set_grant(config, args.name, args.operation == 'grant')
        return
    if args.operation == 'bench':
        import bench
        bench.installed(args)
        return
    if args.operation == 'image':
        host.import_image(args.directory)
        return
    if args.operation == 'install':
        host.install(args)
        return
    if args.operation == 'uninstall':
        host.uninstall(args)
        return
    if args.operation == 'network-up':
        host.network_up(host.load())
        return
    if args.operation in ('new', 'rm'):
        import managed
        config = host.load()
        if args.operation == 'new':
            trace = getattr(args, 'startup_trace', None)
            if trace is not None:
                trace.mark('provision.begin')
            def launch(runtime, record):
                if trace is not None:
                    trace.mark('provision.end')
                connection = runtime._start(args.name, multiplex=not args.no_attach, record=record)
                if not args.no_attach:
                    runtime._attach(args.name, [], connection)
            managed.provision(config, args.name, trace=trace, launch=launch)
        else:
            identifier(args.name, r'[a-z][a-z0-9-]{0,30}')
            if not args.yes and input('Delete disk for ' + args.name + '? Type delete: ') != 'delete':
                raise ValueError('Deletion cancelled')
            managed.delete_machine(config, args.name)
            print(args.name + ' removed')
        return
    runtime = Runtime(args.state, trace=getattr(args, 'startup_trace', None))
    if args.operation == "configure":
        runtime.configure(args)
        print(args.name + " configured")
    elif args.operation == "start":
        runtime.start(args.name, args.timings, args.verbose_boot)
    elif args.operation == "attach":
        runtime.attach(args.name, args.command)
    elif args.operation == "stop":
        runtime.stop(args.name)
    else:
        runtime.ls()

if __name__ == "__main__":
    main()
