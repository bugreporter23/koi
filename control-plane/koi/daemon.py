"""Persistent management endpoint with root or explicitly granted local access."""

import argparse
import json
import os
import pwd
import signal
import socket
import socketserver
import struct
import sys
import threading
from pathlib import Path

from .common import Refused, config, require
from .gate import perform

SOCKET = Path("/run/koi/control.sock")
REQUEST_LIMIT = 65536
RESPONSE_LIMIT = 1024 * 1024


def request_local(request, socket_path=None):
    if socket_path is None:
        require(os.geteuid() == 0, "use sudo to authorize Koi management")
        socket_path = SOCKET
    require(Path(socket_path).is_absolute(), "local socket path must be absolute")
    payload = json.dumps(request).encode() + b"\n"
    require(len(payload) <= REQUEST_LIMIT, "request too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(300)
        try:
            connection.connect(str(socket_path))
        except (FileNotFoundError, ConnectionRefusedError) as error:
            raise Refused("Koi daemon is unavailable; start koi.service") from error
        connection.sendall(payload)
        with connection.makefile("rb") as stream:
            reply = stream.readline(RESPONSE_LIMIT + 1)
    require(
        len(reply) <= RESPONSE_LIMIT and reply.endswith(b"\n"),
        "incomplete or oversized Koi daemon response",
    )
    response = json.loads(reply)
    require(response["ok"], response.get("error", "Koi request failed"))
    return response["result"]


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        try:
            _, uid, _ = struct.unpack(
                "3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            require(uid in (0, self.server.local_uid), "local user is not authorized")
            line = self.rfile.readline(REQUEST_LIMIT + 1)
            require(
                len(line) <= REQUEST_LIMIT and line.endswith(b"\n"),
                "incomplete or oversized Koi request",
            )
            result = perform(json.loads(line))
            response = {"ok": True, "result": result}
        except (Refused, OSError, ValueError, KeyError, TypeError) as error:
            response = {"ok": False, "error": str(error)}
        self.wfile.write(json.dumps(response).encode() + b"\n")


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    request_queue_size = 8

    def __init__(self, *args, local_uid=0, **kwargs):
        self.local_uid = local_uid
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b'{"ok":false,"error":"Koi is busy; retry"}\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local-user", help="Grant direct management to this local user"
    )
    args = parser.parse_args()
    require(os.geteuid() == 0, "Koi daemon requires root")
    uid = pwd.getpwnam(args.local_user).pw_uid if args.local_user else 0
    config()
    os.umask(0o077)
    directory_mode = 0o755 if uid else 0o700
    SOCKET.parent.mkdir(mode=directory_mode, parents=True, exist_ok=True)
    SOCKET.parent.chmod(directory_mode)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    with Server(str(SOCKET), Handler, local_uid=uid) as server:
        try:
            os.chown(SOCKET, uid, 0)
            SOCKET.chmod(0o600)
            print("Koi listening on " + str(SOCKET), flush=True)
            server.serve_forever()
        finally:
            SOCKET.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
