"""Unprivileged, socket-activated inference relay. No controller operations."""

import argparse
import http.client
import http.server
import os
from pathlib import Path
import re
import socket
import socketserver
import threading

from inference_adapters import codex, claude

ADAPTERS = {adapter.NAME: adapter for adapter in (codex, claude)}
MAX_BODY = 32 * 1024**2
REQUEST_HEADERS = {
    'accept', 'accept-encoding', 'content-type', 'user-agent', 'openai-beta',
    'x-client-request-id', 'session_id', 'conversation_id',
    'x-codex-turn-metadata', 'x-codex-turn-state', 'x-codex-beta-features',
    'anthropic-version', 'anthropic-beta', 'x-app',
}
RESPONSE_HEADERS = {
    'content-type', 'content-encoding', 'cache-control', 'retry-after',
    'x-request-id', 'request-id', 'openai-processing-ms',
    'x-ratelimit-limit-requests', 'x-ratelimit-limit-tokens',
    'x-ratelimit-remaining-requests', 'x-ratelimit-remaining-tokens',
    'x-ratelimit-reset-requests', 'x-ratelimit-reset-tokens',
    'anthropic-ratelimit-requests-limit', 'anthropic-ratelimit-requests-remaining',
    'anthropic-ratelimit-requests-reset', 'anthropic-ratelimit-tokens-limit',
    'anthropic-ratelimit-tokens-remaining', 'anthropic-ratelimit-tokens-reset',
}


class BadBody(ValueError):
    def __init__(self, status=400):
        self.status = status


class RelayServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, address, credentials, *, listener=None, adapters=None, connect=None):
        self.credentials = Path(credentials)
        self.adapters = ADAPTERS if adapters is None else adapters
        self.connect = connect or (lambda host: http.client.HTTPSConnection(host, timeout=30))
        self.slots = threading.BoundedSemaphore(64)
        super().__init__(address, RelayHandler, bind_and_activate=listener is None)
        if listener is not None:
            self.socket.close()
            self.socket = listener

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        pass


class RelayHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'koi-relay'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *_):
        pass

    def handle_expect_100(self):
        self.send_error(417)
        return False

    def exact(self, length):
        value = self.rfile.read(length)
        if len(value) != length:
            raise BadBody()
        return value

    def line(self):
        line = self.rfile.readline(8193)
        if len(line) > 8192 or not line.endswith(b'\r\n'):
            raise BadBody()
        return line[:-2]

    def read_body(self):
        lengths = self.headers.get_all('Content-Length', [])
        transfers = self.headers.get_all('Transfer-Encoding', [])
        if transfers:
            if lengths or len(transfers) != 1 or transfers[0].lower() != 'chunked':
                raise BadBody()
            body = bytearray()
            while True:
                text = self.line().split(b';', 1)[0]
                if not re.fullmatch(b'[0-9a-fA-F]{1,16}', text):
                    raise BadBody()
                size = int(text, 16)
                if len(body) + size > MAX_BODY:
                    raise BadBody(413)
                if size == 0:
                    total = 0
                    while True:
                        trailer = self.line()
                        total += len(trailer) + 2
                        if total > 8192:
                            raise BadBody()
                        if not trailer:
                            return bytes(body)
                body.extend(self.exact(size))
                if self.exact(2) != b'\r\n':
                    raise BadBody()
        if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,16}', lengths[0]):
            raise BadBody()
        length = int(lengths[0])
        if length > MAX_BODY:
            raise BadBody(413)
        return self.exact(length)

    def do_POST(self):
        self.close_connection = True
        target = self.requestline.split()[1]
        match = re.fullmatch(r'/(codex|claude)(/[^?]*)(?:\?([^\r\n#]*))?', target)
        if not match:
            self.send_error(403, 'not an allowed model endpoint')
            return
        name, path, query = match.groups()
        adapter = self.server.adapters[name]
        if path not in adapter.PATHS:
            self.send_error(403, 'not an allowed model endpoint')
            return
        try:
            body = self.read_body()
        except BadBody as error:
            self.send_error(error.status, 'invalid or oversized request body')
            return
        except (OSError, ValueError):
            self.send_error(400, 'incomplete request body')
            return
        nominated = {v.strip().lower() for v in self.headers.get('Connection', '').split(',')}
        incoming = {k.lower(): v for k, v in self.headers.items()
                    if k.lower() in REQUEST_HEADERS and k.lower() not in nominated}
        target = adapter.UPSTREAM_PREFIX + path + ('?' + query if query else '')
        upstream = None
        sent = False
        try:
            for attempt in (0, 1):
                try:
                    headers = adapter.headers(self.server.credentials, incoming, force=bool(attempt))
                except (OSError, ValueError, KeyError, RuntimeError):
                    self.send_error(502, 'host login unavailable; run koi login ' + name)
                    return
                upstream = self.server.connect(adapter.UPSTREAM)
                upstream.request('POST', target, body, headers)
                if upstream.sock is not None:
                    upstream.sock.settimeout(600)
                response = upstream.getresponse()
                if response.status == 401 and attempt == 0 and adapter.REFRESHABLE:
                    upstream.close()
                    continue
                break
            if response.status == 401:
                self.send_error(502, 'host login rejected; run koi login ' + name)
                return
            nominated = {v.strip().lower() for v in response.getheader('Connection', '').split(',')}
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() in RESPONSE_HEADERS and key.lower() not in nominated:
                    self.send_header(key, value)
            self.send_header('Connection', 'close')
            self.end_headers()
            sent = True
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            if not sent:
                self.send_error(502, 'inference upstream unreachable')
        finally:
            if upstream is not None:
                upstream.close()


def activated_listener():
    if os.environ.get('LISTEN_PID') != str(os.getpid()) or os.environ.get('LISTEN_FDS') != '1':
        raise ValueError('Relay requires exactly one systemd-activated socket')
    listener = socket.socket(fileno=3)
    if listener.family != socket.AF_UNIX or listener.type != socket.SOCK_STREAM or not listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
        listener.close()
        raise ValueError('Relay requires a listening Unix stream socket')
    listener.set_inheritable(False)
    return listener


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    with RelayServer(None, args.credentials, listener=activated_listener()) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
