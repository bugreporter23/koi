import fcntl
import hashlib
import ipaddress
import json
import os
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import guest_access, network
from .common import Refused, config, integer, require, token
from .incus import Incus
from .storage import storage, storage_usage

STATE = Path("/var/lib/koi")
SCHEMA = {
    "list": set(),
    "status": set(),
    "network": set(),
    "create": {"request", "name", "image", "size"},
    "start": {"id"},
    "stop": {"id"},
    "destroy": {"id"},
    "endpoint": {"id"},
    "guest-access": {"id", "publicKey"},
    "snapshot": {"id", "snapshot"},
    "restore": {"id", "snapshot"},
}


def operation(request):
    require(type(request) is dict, "request must be an object")
    op = request.get("op")
    require(isinstance(op, str) and op in SCHEMA, "operation not allowed")
    fields = set(request)
    if op in ("stop", "destroy"):
        fields.discard("force")
        require(type(request.get("force", False)) is bool, "force must be Boolean")
    require(fields == SCHEMA[op] | {"op"}, "unexpected or missing fields")
    return op


@contextmanager
def operation_lock(state, *, shared=False):
    state.mkdir(mode=0o700, exist_ok=True)
    with (state / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield


def identity(value):
    token(value, r"[0-9a-f]{32}")
    return value


def facts(instance, c):
    require(
        instance["type"] == "virtual-machine",
        "unknown object in Koi project; protected",
    )
    try:
        g = json.loads(instance["config"]["user.koi"])
        require(
            set(g)
            == {
                "id",
                "instance",
                "request",
                "requestHash",
                "name",
                "nic",
                "mac",
                "address",
                "cpu",
                "memoryMiB",
                "diskGiB",
            },
            "unsupported VM metadata; recreate experimental guests explicitly",
        )
        identity(g["id"])
        identity(g["request"])
        token(g["requestHash"], r"[0-9a-f]{64}")
        token(g["name"])
        require(
            instance["name"] == g["instance"] == "koi-" + g["request"],
            "identity mismatch",
        )
        require(g["nic"] == "koi" + g["id"][:12], "NIC identity mismatch")
        token(g["mac"], r"02(?::[0-9a-f]{2}){5}")
        require(
            ipaddress.IPv4Address(g["address"]) in ipaddress.IPv4Network(c["prefix"]),
            "address outside allocation",
        )
        for k in ("cpu", "memoryMiB", "diskGiB"):
            integer(g[k], 1, 262144)
        g["status"] = instance["status"]
        return g
    except (KeyError, ValueError, TypeError) as e:
        raise Refused("missing/corrupt VM identity; object is protected") from e


class Gate:
    def __init__(self, c, api=None, state=STATE):
        self.c, self.api, self.state = c, api or Incus(), state

    def guests(self):
        gs = [facts(i, self.c) for i in self.api.instances()]
        for field in ("id", "address", "nic", "name"):
            require(
                len({g[field] for g in gs}) == len(gs), "duplicate VM facts; protected"
            )
        return gs

    def reconcile(self, gs=None):
        if gs is None:
            gs = self.guests()
        network.local_hosts(self.c, gs)

    def available(self):
        require(
            not (self.state / "maintenance").exists(),
            "host maintenance paused management; administrator must explicitly resume",
        )

    def guest(self, identifier, gs):
        identity(identifier)
        found = [g for g in gs if g["id"] == identifier]
        require(len(found) == 1, "VM unavailable")
        return found[0]

    def endpoint(self, identifier, public_key=None):
        if public_key is not None:
            public_key = guest_access.public_key(public_key)
        with operation_lock(self.state, shared=True):
            self.available()
            g = self.guest(identifier, self.guests())
            require(g["status"] == "Running", "VM is not running")
        key = self.api.file(g["instance"], "/etc/ssh/ssh_host_ed25519_key.pub").strip()
        token(key, r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?")
        wait_ssh(g["address"])
        with operation_lock(self.state, shared=True):
            self.available()
            current = facts(self.api.instance(g["instance"]), self.c)
            require(
                current == g, "VM changed while waiting for its SSH identity; retry"
            )
            result = {
                "id": g["id"],
                "host": network.hostname(self.c, g),
                "address": g["address"],
                "user": "dev",
                "port": 22,
                "hostKey": key,
            }
            if public_key is not None:
                result["certificate"] = guest_access.sign(
                    self.state, identifier, public_key
                )
            return result

    def execute(self, request):
        op = operation(request)
        if op == "list":
            return self.guests()
        if op == "network":
            return {
                **network.status(self.c),
                "mode": self.c["networkMode"],
                "mtu": self.c["mtu"],
            }
        if op == "status":
            return {
                "host": self.c["host"],
                "network": network.status(self.c),
                "storage": storage_usage(self.c, self.api),
                "maintenance": (self.state / "maintenance").exists(),
            }
        if op == "endpoint":
            return self.endpoint(request["id"])
        if op == "guest-access":
            return self.endpoint(
                request["id"], guest_access.public_key(request["publicKey"])
            )
        self.available()
        gs = self.guests()
        if op == "create":
            return self.create(request, gs)
        g = self.guest(request["id"], gs)
        name = g["instance"]
        if op in ("snapshot", "restore"):
            token(request["snapshot"])
            require(g["status"] == "Stopped", "stop the VM before snapshot/restore")
            storage(self.c, self.api)
            if op == "snapshot":
                self.api.request(
                    "POST",
                    self.api.path(name) + "/snapshots?project=koi",
                    {"name": request["snapshot"], "stateful": False},
                )
            else:
                self.api.request(
                    "PUT",
                    self.api.path(name) + "?project=koi",
                    {"restore": request["snapshot"]},
                )
            return {"id": g["id"], "completed": op}
        if op == "destroy":
            if g["status"] != "Stopped":
                require(
                    request.get("force", False),
                    "explicitly stop before destroy or use --force",
                )
                self.api.action(name, "stop", force=True)
            self.api.request("DELETE", self.api.path(name) + "?project=koi")
            self.reconcile([v for v in gs if v["id"] != g["id"]])
            return {"id": g["id"], "destroyed": True}
        if op == "start":
            storage(self.c, self.api)
            self.reconcile(gs)
        if (op == "start" and g["status"] != "Running") or (
            op == "stop" and g["status"] != "Stopped"
        ):
            if op == "stop" and request.get("force", False):
                self.api.action(name, op, force=True)
            else:
                self.api.action(name, op)
        current = facts(self.api.instance(name), self.c)
        require(current["id"] == g["id"], "VM identity changed")
        return current

    def create(self, r, gs):
        request_id = identity(r["request"])
        token(r["name"])
        require(
            r["image"] in self.c["images"] and r["size"] in self.c["sizes"],
            "image/size outside envelope",
        )
        digest = hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest()
        existing = [g for g in gs if g["request"] == request_id]
        if existing:
            g = existing[0]
            require(
                g["requestHash"] == digest,
                "request ID already used",
            )
            return g                                                                  
        require(
            all(g["name"] != r["name"] for g in gs), "friendly name already allocated"
        )
        operations = self.api.request("GET", "/1.0/operations?recursion=1&project=koi")
        require(
            all(
                op["status_code"] >= 200
                for group in operations.values()
                for op in group
            ),
            "Incus operation still pending; inspect/retry after it completes",
        )
        size = self.c["sizes"][r["size"]]
        storage(self.c, self.api)
        addresses = [str(a) for a in ipaddress.IPv4Network(self.c["prefix"]).hosts()]
        addresses = addresses[1:]                                           
        available = [a for a in addresses if a not in {g["address"] for g in gs}]
        require(bool(available), "guest addresses exhausted")
        address = available[0]
        identifier = uuid.uuid4().hex
        mac = "02:" + ":".join(identifier[i : i + 2] for i in range(0, 10, 2))
        g = {
            "id": identifier,
            "instance": "koi-" + request_id,
            "name": r["name"],
            "address": address,
            "nic": "koi" + identifier[:12],
            "mac": mac,
            "requestHash": digest,
            "request": request_id,
            **size,
        }
        _, ca = guest_access.authority(self.state, create=True)
        nic = {
            "type": "nic",
            "name": "eth0",
            "host_name": g["nic"],
            "hwaddr": mac,
        }
        nic["network"] = self.c["bridge"]
        nic["ipv4.address"] = address
        fingerprint = self.c["images"][r["image"]]
        config = {
            "user.koi": json.dumps(g),
            "limits.cpu": str(size["cpu"]),
            "limits.memory": str(size["memoryMiB"]) + "MiB",
            "security.secureboot": "false",
        }
        devices = {
            "root": {
                "type": "disk",
                "path": "/",
                "pool": self.c["pool"],
                "size": str(size["diskGiB"]) + "GiB",
            },
            "eth0": nic,
        }
        properties = self.api.request(
            "GET", "/1.0/images/" + fingerprint + "?project=koi"
        )["properties"]
        from koi_guest.boot import configure

        configure(
            properties,
            config,
            name=r["name"],
            ca=ca,
            identifier=identifier,
        )
        self.api.request(
            "POST",
            "/1.0/instances?project=koi",
            {
                "name": g["instance"],
                "type": "virtual-machine",
                "profiles": [],
                "source": {"type": "image", "fingerprint": fingerprint},
                "config": config,
                "devices": devices,
            },
        )
        gs = self.guests()
        self.reconcile(gs)
        return self.guest(identifier, gs)


def wait_ssh(address):
    deadline = time.monotonic() + 30
    while True:
        try:
            with socket.create_connection(
                (address, 22),
                timeout=min(0.05, max(0.001, deadline - time.monotonic())),
            ):
                return
        except OSError as error:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Refused(
                    f"SSH at {address}:22 did not become ready within 30 seconds"
                ) from error
            time.sleep(min(0.01, remaining))


def perform(request):
    """Serialize mutations; endpoint readiness waits hold no operation lock."""
    require(os.geteuid() == 0, "use sudo to authorize Koi management")
    c = config()
    op = operation(request)
    gate = Gate(c, state=STATE)
    if op in ("endpoint", "guest-access"):
        return gate.execute(request)
    with operation_lock(STATE, shared=op in ("list", "status", "network")):
        return gate.execute(request)


def read_request():
    line = sys.stdin.buffer.readline(65537)
    require(len(line) <= 65536, "request too large")
    return json.loads(line)


def main():
    try:
        require(
            os.geteuid() == 0 and len(sys.argv) == 1,
            "gate requires its fixed privileged entry point",
        )
        principal = os.environ.get("SUDO_USER", "")
        require(
            principal == "koi-manager",
            "use the authenticated Koi receiving account",
        )
        result = perform(read_request())
        print(json.dumps({"ok": True, "result": result}))
    except (Refused, OSError, ValueError, KeyError, TypeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
