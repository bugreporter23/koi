"""Incus's public local REST API; no database or disk inspection."""

import http.client
import json
import socket
import time
from urllib.parse import quote

from .common import Refused

AGENT_POLL_INTERVAL = 0.1


class AgentUnavailable(Refused):
    pass


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/lib/incus/unix.socket")


class Incus:
    def request(self, method, path, body=None, raw=False, timeout=180):
        conn = UnixHTTP("localhost", timeout=timeout)
        try:
            conn.request(
                method,
                path,
                None if body is None else json.dumps(body),
                {"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            data = response.read()
            if response.status >= 400:
                if (
                    response.status == 500
                    and json.loads(data).get("error")
                    == "VM agent isn't currently running"
                ):
                    raise AgentUnavailable("VM agent isn't currently running")
                raise Refused(f"Incus {method} {path}: {data.decode()[:1500]}")
            if raw:
                return data.decode()
            result = json.loads(data)
        finally:
            conn.close()
        if result.get("type") == "error":
            raise Refused(f"Incus {method} {path}: {result.get('error')}")
        if result.get("type") == "async":
            operation = result["operation"].split("?")[0]
            result = self.request("GET", operation + "/wait?timeout=120")
            if result["status_code"] != 200:
                raise Refused(
                    f"Incus operation incomplete: {operation}: {result.get('err')}"
                )
            return result
        return result["metadata"]

    def instances(self):
        return self.request("GET", "/1.0/instances?recursion=1&project=koi")

    def instance(self, name):
        return self.request("GET", self.path(name) + "?project=koi")

    def path(self, name):
        return "/1.0/instances/" + quote(name, safe="")

    def action(self, name, action, *, force=False):
        return self.request(
            "PUT",
            self.path(name) + "/state?project=koi",
            {"action": action, "timeout": 60, "force": force},
        )

    def file(self, name, path):
        deadline = time.monotonic() + 30
        while True:
            try:
                return self.request(
                    "GET",
                    self.path(name) + "/files?project=koi&path=" + quote(path),
                    raw=True,
                    timeout=max(0.1, deadline - time.monotonic()),
                )
            except AgentUnavailable as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Refused(
                        f"VM agent for {name} did not become ready within 30 seconds"
                    ) from error
                time.sleep(min(AGENT_POLL_INTERVAL, remaining))
