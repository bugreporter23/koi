"""Pinned guest SSH, readiness notification and ordinary ACPI shutdown."""

from contextlib import closing
import ipaddress
import json
import shlex
import socket
import subprocess
import time

import trial


def identity(packet, seed, expected_key=None, address=None):
    payload = json.loads(packet)
    result = trial.guest.validate(payload)
    if (result["id"], result["name"]) != (seed["id"], seed["name"]):
        raise ValueError("Readiness identity does not match the retained seed")
    if expected_key is not None and result["ssh_key"] != expected_key:
        raise ValueError("Guest SSH host key changed across restart")
    if address is not None and payload.get("address") != address:
        raise ValueError("Guest address differs from the prepared network assignment")
    return result["ssh_key"]


def receive(listener, seed, expected_key=None, timeout=90, address=None, trace=None):
    deadline = time.monotonic() + timeout
    listener.settimeout(timeout)
    connection, _ = listener.accept()
    with connection:
        if trace is not None:
            host_send = time.monotonic_ns()
            connection.sendall(b'T')
        packet = bytearray()
        while not packet.endswith(b"\n"):
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            block = connection.recv(4096 - len(packet))
            if not block:
                raise ValueError("Incomplete guest readiness message")
            packet.extend(block)
            if len(packet) >= 4096:
                raise ValueError("Oversized guest readiness message")
        host_receive = time.monotonic_ns()
    key = identity(packet, seed, expected_key, address)
    if trace is not None:
        import startup_trace
        clocks = json.loads(packet).get('trace', {})
        exchange = {'host_send_ns': host_send, 'host_receive_ns': host_receive,
                    'guest_receive_ns': clocks.get('receive_ns'), 'guest_send_ns': clocks.get('send_ns')}
        startup_trace.offset(exchange)                                                              
        trace.calibration = exchange
        trace.mark('ready.received', host_receive)
        trace.mark('ready.validated')
    return key


def ssh_command(args, script):
    address = str(ipaddress.IPv4Address(args.address))
    proxy = shlex.join(['socat', '-', 'TCP4:' + address + ':22,nodelay,connect-timeout=5'])
    argv = [
        "ssh", "-F", "/dev/null", "-i", str(getattr(args, "key", args.work / "management-key")),
        "-o", "ProxyCommand=" + proxy,
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "IdentityAgent=none", "-o", "ForwardAgent=no",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "UserKnownHostsFile=" + str(args.work / "known_hosts"),
        "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5",
        "dev@" + args.address, script,
    ]
    if getattr(args, 'certificate', None) is not None:
        argv[1:1] = ['-o', 'CertificateFile=' + str(args.certificate)]
    if getattr(args, "control", None) is not None:
        argv[1:1] = ["-o", "ControlMaster=auto", "-o", "ControlPersist=5",
                     "-o", "ControlPath=" + str(args.control)]
    return argv


def ssh(args, script, check=True, timeout=30):
    return subprocess.run(ssh_command(args, script), check=check,
                          stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=timeout)


def services_ready(args):
    """Acceptance milestone after entry; never part of interactive startup."""
    script = """for attempt in $(seq 1 150); do
    if sudo -n udevadm control --timeout=1 --reload >/dev/null 2>&1 &&
       nix store ping --store daemon >/dev/null 2>&1; then exit 0; fi
    sleep 0.1
done
echo 'ordinary guest services did not become ready after entry' >&2
exit 1
"""
    return ssh(args, script, timeout=25)


def request_poweroff(runtime):
    import http.client

    class LocalHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(str(self.host))

    with closing(LocalHTTP(str(runtime / "api"), timeout=5)) as connection:
        connection.request("PUT", "/api/v1/vm.power-button")
        response = connection.getresponse()
        response.read()
        if response.status != 204:
            raise RuntimeError("Guest power-button request failed: " + str(response.status))


