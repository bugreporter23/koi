"""Provider-local SSH authority for authorized VM use."""

import base64
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
import tempfile
import time

from .common import atomic, require, run, token

LIFETIME = 300


def public_key(value):
    token(value, r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n\x00]*)?")
    require(len(value) <= 1024, "guest public key too large")
    blob = base64.b64decode(value.split()[1], validate=True)
    require(
        len(blob) == 51 and blob[:19] == b"\0\0\0\x0bssh-ed25519\0\0\0\x20",
        "expected a plain Ed25519 guest public key",
    )
    return " ".join(value.split()[:2])


def authority(state, *, create=False):
    directory = Path(state) / "guest-access"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.stat()
    require(
        info.st_uid == os.geteuid() and not info.st_mode & 0o077,
        "unsafe guest CA directory",
    )
    key = directory / "ca"
    if create and not key.exists():
        require(not key.with_suffix(".pub").exists(), "incomplete guest CA")
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            generated = Path(temporary) / "ca"
            run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(generated))
            atomic(key, generated.read_text())
            atomic(key.with_suffix(".pub"), generated.with_suffix(".pub").read_text())
    info = key.stat()
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.geteuid()
        and not info.st_mode & 0o077,
        "unsafe guest CA key",
    )
    public = public_key(key.with_suffix(".pub").read_text().strip())
    require(
        public_key(run("ssh-keygen", "-y", "-f", str(key)).stdout.strip()) == public,
        "guest CA key mismatch",
    )
    return key, public


def sign(state, identifier, key):
    token(identifier, r"[0-9a-f]{32}")
    key = public_key(key)
    ca, _ = authority(state)
    now = int(time.time())

    def date(timestamp):
        return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y%m%d%H%M%SZ")

    with tempfile.TemporaryDirectory(dir=ca.parent) as temporary:
        public = Path(temporary) / "client.pub"
        public.write_text(key + "\n")
        run(
            "ssh-keygen",
            "-q",
            "-s",
            str(ca),
            "-I",
            "koi-" + identifier,
            "-n",
            "koi-" + identifier,
            "-V",
            date(now - 30) + ":" + date(now + LIFETIME),
            "-O",
            "clear",
            "-O",
            "permit-pty",
            str(public),
        )
        return public.with_name("client-cert.pub").read_text().strip()


def guest_files(ca, identifier):
    token(identifier, r"[0-9a-f]{32}")
    return {
        "/etc/ssh/koi-ca.pub": public_key(ca) + "\n",
        "/etc/ssh/koi-principals": "koi-" + identifier + "\n",
        "/etc/ssh/sshd_config.d/00-koi-access.conf": (
            "TrustedUserCAKeys /etc/ssh/koi-ca.pub\n"
            "AuthorizedPrincipalsFile /etc/ssh/koi-principals\n"
            "AllowUsers dev\nPasswordAuthentication no\n"
            "KbdInteractiveAuthentication no\nPermitRootLogin no\n"
        ),
    }
