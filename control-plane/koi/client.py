import argparse
import base64
import fcntl
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from .common import Refused, atomic, require
from .daemon import request_local
from .guest_access import public_key


def endpoint(value, identifier):
    """Validate authenticated endpoint facts before using them as SSH arguments."""
    require(isinstance(value, dict), "invalid guest endpoint")
    require(
        isinstance(identifier, str) and re.fullmatch(r"[0-9a-f]{32}", identifier),
        "expected immutable VM ID",
    )
    require(value.get("id") == identifier, "guest endpoint ID mismatch")
    for field, pattern in (
        ("host", r"[a-z0-9-]+(?:\.[a-z0-9-]+)+"),
        ("user", r"[a-z_][a-z0-9_-]{0,31}"),
        ("hostKey", r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n\x00]*)?"),
    ):
        text = value.get(field)
        require(
            isinstance(text, str) and len(text) <= 1024 and re.fullmatch(pattern, text),
            "invalid guest endpoint " + field,
        )
    require(
        type(value.get("port")) is int and 1 <= value["port"] <= 65535,
        "invalid guest endpoint port",
    )
    require(isinstance(value.get("address"), str), "invalid guest endpoint address")
    ipaddress.IPv4Address(value["address"])
    key = base64.b64decode(value["hostKey"].split()[1], validate=True)
    require(
        len(key) == 51 and key[:19] == b"\0\0\0\x0bssh-ed25519\0\0\0\x20",
        "invalid guest Ed25519 key",
    )
    return value


def pin_guest(value):
    """Koi trust state is independent of any terminal launcher's inventory."""
    path = Path.home() / ".local/state/koi/known_hosts"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    alias = "koi-" + value["id"]
    entry = alias + " " + value["hostKey"] + "\n"
    with path.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = path.read_text() if path.exists() else ""
        matches = [line for line in old.splitlines() if line.startswith(alias + " ")]
        require(
            not matches or matches == [entry.strip()],
            "guest host key changed; investigate before accepting it",
        )
        if not matches:
            atomic(path, old + entry)
    return alias, path


def connection_options(value):
    alias, path = pin_guest(value)
    return (
        "StrictHostKeyChecking=yes",
        "GlobalKnownHostsFile=/dev/null",
        "KnownHostsCommand=none",
        "VerifyHostKeyDNS=no",
        "UpdateHostKeys=no",
        "CheckHostIP=no",
        "ForwardAgent=no",
        "ForwardX11=no",
        "ForwardX11Trusted=no",
        "ClearAllForwardings=yes",
        "Tunnel=no",
        "ControlMaster=no",
        "ControlPath=none",
        "ControlPersist=no",
        "ProxyCommand=none",
        "ProxyJump=none",
        "CanonicalizeHostname=no",
        "PermitLocalCommand=no",
        "RemoteCommand=none",
        "SessionType=default",
        "ConnectTimeout=10",
        "ConnectionAttempts=1",
        "HostKeyAlias=" + alias,
        "UserKnownHostsFile=" + str(path),
        "HostName=" + value["address"],
    )


def ssh_quote(value):
    value = str(value)
    require(not any(c in value for c in '\n\r\0"\\'), "invalid SSH configuration value")
    return '"' + value.replace("%", "%%") + '"'


def authentication_options(credentials):
    key, certificate = credentials
    return (
        "IdentityFile=" + str(key),
        "CertificateFile=" + str(certificate),
        "IdentityAgent=none",
        "IdentitiesOnly=yes",
        "PreferredAuthentications=publickey",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
    )


def client_key(directory):
    key = directory / "key"
    if not key.exists():
        result = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
            text=True,
            capture_output=True,
            check=False,
        )
        require(result.returncode == 0, "guest key generation failed: " + result.stderr)
    info = key.stat()
    require(
        info.st_uid == os.getuid() and not info.st_mode & 0o077,
        "unsafe client guest key",
    )
    result = subprocess.run(
        ["ssh-keygen", "-y", "-f", str(key)],
        text=True,
        capture_output=True,
        check=False,
    )
    require(result.returncode == 0, "guest public key unavailable: " + result.stderr)
    return key, public_key(result.stdout.strip())


def acquire(host, identifier, directory):
    key, public = client_key(directory)
    value = endpoint(
        invoke(host, {"op": "guest-access", "id": identifier, "publicKey": public}),
        identifier,
    )
    certificate = value.get("certificate")
    require(
        isinstance(certificate, str)
        and len(certificate) <= 16384
        and re.fullmatch(
            r"ssh-ed25519-cert-v01@openssh.com [A-Za-z0-9+/=]+(?: [^\r\n\x00]*)?",
            certificate,
        ),
        "invalid guest SSH certificate",
    )
    path = directory / "key-cert.pub"
    atomic(path, certificate + "\n")
    return value, (key, path)


def bind_guest(value, identifier, destination, policy, credentials):
    """Export a pinned connection for ordinary SSH without fabric authorization."""
    value = endpoint(value, identifier)
    if policy is not None:
        policy = Path(policy).resolve(strict=True)
        require(policy.is_file(), "guest SSH policy does not exist")
    destination = Path(destination).absolute()
    require(
        destination != policy, "connection export would overwrite authentication policy"
    )
    options = (*connection_options(value), *authentication_options(credentials))
    text = "Host *\n"
    for option in (*options, "User=" + value["user"], "Port=" + str(value["port"])):
        key, setting = option.split("=", 1)
        encoded = (
            setting
            if key in ("ProxyCommand", "RemoteCommand", "KnownHostsCommand")
            else ssh_quote(setting)
        )
        text += "  " + key + " " + encoded + "\n"
    if policy is not None:
        text += "  Include " + ssh_quote(policy) + "\n"
    atomic(destination, text)
    return {"id": identifier, "sshConfig": str(destination)}


def guest_ssh(value, identifier, credentials, policy=None, command=None):
    value = endpoint(value, identifier)
    if command is not None:
        require(bool(command), "exec-guest requires a command after --")
        require(
            all(isinstance(v, str) and "\x00" not in v for v in command),
            "invalid guest command",
        )
    if policy is not None:
        require(Path(policy).is_absolute(), "guest SSH policy must be absolute")
        require(Path(policy).is_file(), "guest SSH policy does not exist")
    argv = ["ssh", "-F", policy or "/dev/null"]
    if command is not None:
        argv += [
            "-T",
            "-oBatchMode=yes",
            "-oRequestTTY=no",
            "-oStdinNull=no",
        ]
    argv += [
        "-o" + option
        for option in (*connection_options(value), *authentication_options(credentials))
    ]
    argv += ["-l", value["user"], "-p", str(value["port"]), value["host"]]
    if command is not None:
        argv.append(shlex.join(command))
    return argv


def show(value, request, host):
    op = request["op"]
    print(f"Koi · {host}")
    if op == "list":
        if not value:
            print("No VMs.")
            return
        print(f"{'NAME':20} {'STATE':10} {'ADDRESS':15} ID")
        for guest in value:
            print(
                f"{guest['name']:20} {guest['status']:10} {guest['address']:15} {guest['id']}"
            )
    elif op == "status":
        print(f"Host: {value['host']}")
        print(
            "Management: "
            + ("paused for maintenance" if value["maintenance"] else "available")
        )
        storage = value["storage"]
        print(f"Storage data used: {storage['usedPercent']:.1f}%")
        print("Network interface: " + value["network"]["interface"])
        print("Status does not test fresh guest connectivity.")
    elif op == "network":
        print(
            f"Local network: {value['prefix']} via {value['interface']} ({value['mode']})"
        )
    elif op == "create":
        print(f"Created {value['name']} · {value['status']}\nVM ID: {value['id']}")
        selector = [] if host == "local" else ["--host", host]
        print("Next: " + shlex.join(["koi", *selector, "start", value["id"]]))
    elif op in ("endpoint", "guest-access"):
        print(
            f"VM ID: {value['id']}\nHost: {value['host']}\nSSH: {value['user']}@{value['address']}:{value['port']}"
        )
        print("Guest host key: " + value["hostKey"])
        if op == "guest-access":
            print("Guest SSH certificate: " + value["certificate"])
    else:
        print(f"{op.capitalize()} completed · {value['id']}")
        if op == "start":
            print(
                "Guest power is running; guest boot and SSH readiness have not been checked."
            )
        if value.get("error"):
            print("Error: " + value["error"])
            if "projection" in value:
                print("Network projection: " + value["projection"])


def invoke(host, request):
    local_socket = os.environ.get("KOI_LOCAL_SOCKET")
    if host == "local" and local_socket:
        return request_local(request, socket_path=local_socket)
    helper = shutil.which("koi-admin")
    require(helper, "install the Koi administration companion")
    result = subprocess.run(
        [
            "sudo",
            str(Path(helper).resolve()),
            host,
        ],
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    require(
        bool(result.stdout), "Koi administration entry point failed: " + result.stderr
    )
    response = json.loads(result.stdout)
    require(response["ok"], response.get("error", "request failed"))
    require(result.returncode == 0, result.stderr)
    return response["result"]


def parser(add_commands=None):
    p = argparse.ArgumentParser(prog="koi", description="Authorized Koi VM management")
    p.add_argument(
        "--host",
        default="local",
        help="Provisioned remote target (default: local Incus)",
    )
    sub = p.add_subparsers(dest="op", required=True)
    for op in ("list", "status", "network"):
        sub.add_parser(op)
    create = sub.add_parser(
        "create", help="create a stopped VM; save the printed request ID for retry"
    )
    create.add_argument("name")
    create.add_argument("--image", required=True)
    create.add_argument("--size", required=True)
    create.add_argument("--request", default=None)
    for op in (
        "start",
        "stop",
        "destroy",
        "endpoint",
        "guest-access",
        "enter",
        "exec-guest",
        "bind",
        "snapshot",
        "restore",
    ):
        cmd = sub.add_parser(op)
        cmd.add_argument("id", help="VM name or immutable ID")
        if op in ("stop", "destroy"):
            cmd.add_argument(
                "--force",
                action="store_true",
                help=(
                    "Cut power and delete the VM"
                    if op == "destroy"
                    else "Cut power without guest shutdown; unwritten data may be lost"
                ),
            )
        if op == "bind":
            cmd.add_argument("destination", type=Path)
        if op == "guest-access":
            cmd.add_argument("--public-key", required=True, type=Path)
        if op in ("exec-guest", "bind"):
            cmd.add_argument("--guest-ssh-config", help="Optional client SSH settings")
        if op in ("snapshot", "restore"):
            cmd.add_argument("snapshot")
    if add_commands is not None:
        add_commands(sub)
    completion = sub.add_parser(
        "completions", help="Print shell completion without contacting a host"
    )
    completion.add_argument("shell", choices=("zsh",))

    def complete(args):
        from .completion import zsh

        print(zsh(p), end="")

    completion.set_defaults(_execute=complete)
    for cmd in sub.choices.values():
        formatting = cmd.add_mutually_exclusive_group()
        formatting.add_argument(
            "--json", action="store_true", help="Structured output for scripts"
        )
        formatting.add_argument(
            "--human",
            action="store_true",
            help="Readable summary, including when redirected",
        )
    return p


def main(p=None):
    p = p if p is not None else parser()
    arguments = sys.argv[1:]
    command = None
    if "--" in arguments and "exec-guest" in arguments[: arguments.index("--")]:
        split = arguments.index("--")
        command, arguments = arguments[split + 1 :], arguments[:split]
    parsed = p.parse_args(arguments)
    handler = getattr(parsed, "_execute", None)
    if handler is not None:
        handler(parsed)
        return
    args = vars(parsed)
    structured = args.pop("json")
    human = args.pop("human")
    structured = structured or (not human and not sys.stdout.isatty())
    host = args.pop("host")
    require(
        host and not host.startswith("-") and not any(c in host for c in "\n\r\x00"),
        "invalid SSH destination",
    )
    executing = args["op"] == "exec-guest"
    binding = args["op"] == "bind"
    destination = args.pop("destination", None)
    entering = args["op"] in ("enter", "exec-guest")
    require(executing or command is None, "only exec-guest accepts a command")
    policy = args.pop("guest_ssh_config", None)
    public = args.pop("public_key", None)
    if public is not None:
        args["publicKey"] = public_key(public.read_text().strip())
    if executing:
        require(bool(command), "exec-guest requires a command after --")
    if args["op"] == "create":
        args["request"] = args["request"] or uuid.uuid4().hex
        print("create request: " + args["request"], file=sys.stderr, flush=True)
    if "id" in args and not re.fullmatch(r"[0-9a-f]{32}", args["id"]):
        name = args["id"]
        require(re.fullmatch(r"[a-z][a-z0-9-]{0,30}", name), "invalid VM name")
        matches = [g for g in invoke(host, {"op": "list"}) if g["name"] == name]
        require(len(matches) == 1, "VM name unavailable or ambiguous")
        args["id"] = matches[0]["id"]
    if binding:
        destination = destination.absolute()
        directory = destination.with_suffix(destination.suffix + ".credentials")
        directory.mkdir(mode=0o700, exist_ok=True)
        require(
            directory.stat().st_uid == os.getuid()
            and not directory.stat().st_mode & 0o077,
            "unsafe client credential directory",
        )
        with (directory / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            value, credentials = acquire(host, args["id"], directory)
            print(
                json.dumps(
                    bind_guest(value, args["id"], destination, policy, credentials)
                )
            )
        return
    if entering:
        with tempfile.TemporaryDirectory(prefix="koi-ssh-") as directory:
            value, credentials = acquire(host, args["id"], Path(directory))
            result = subprocess.run(
                guest_ssh(value, args["id"], credentials, policy, command), check=False
            )
            sys.exit(result.returncode)
    value = invoke(host, args)
    if structured:
        print(json.dumps(value, indent=2))
    else:
        show(value, args, host)


if __name__ == "__main__":
    try:
        main()
    except (Refused, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
