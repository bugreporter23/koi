"""Sudo entry point on designated administration machines only."""

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from .common import Refused, require, token
from .daemon import request_local
from .gate import read_request
from . import targets


def protected_file(value, private=False):
    path = Path(value).resolve(strict=True)
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0, "root-owned file required")
    require(not info.st_mode & (0o077 if private else 0o022), "unsafe file permissions")
    for parent in path.parents:
        info = parent.stat()
        require(
            info.st_uid == 0
            and (not info.st_mode & 0o022 or info.st_mode & stat.S_ISVTX),
            "unsafe configuration or credential directory",
        )
    return str(path)


def send(target, request):
    require(os.geteuid() == 0, "use sudo to authorize Koi management")
    token(target)
    path = protected_file(targets.PATH)
    configured = targets.validate(json.loads(Path(path).read_text()))
    require(
        target in configured,
        "target is not provisioned for this administration machine",
    )
    config = configured[target]
    key = protected_file(config["identityFile"], private=True)
    with tempfile.TemporaryDirectory(prefix="koi-target-") as temporary:
        known = Path(temporary) / "known_hosts"
        known.write_text(f"[{config['host']}]:{config['port']} {config['serverKey']}\n")
        known.chmod(0o600)
        return subprocess.run(
            [
                "ssh",
                "-F",
                "/dev/null",
                "-T",
                "-oBatchMode=yes",
                "-oStrictHostKeyChecking=yes",
                "-oIdentitiesOnly=yes",
                "-oIdentityAgent=none",
                "-oForwardAgent=no",
                "-oControlPath=none",
                "-oProxyCommand=none",
                "-oProxyJump=none",
                "-oGlobalKnownHostsFile=/dev/null",
                "-oUserKnownHostsFile=" + str(known),
                "-oConnectTimeout=15",
                "-oServerAliveInterval=15",
                "-oServerAliveCountMax=3",
                "-i",
                key,
                "-p",
                str(config["port"]),
                "--",
                "koi-manager@" + config["host"],
            ],
            input=json.dumps(request) + "\n",
            text=True,
            capture_output=True,
            check=False,
        )


def main():
    try:
        require(len(sys.argv) == 2, "expected one provisioned target")
        request = read_request()
        if sys.argv[1] == "local":
            print(json.dumps({"ok": True, "result": request_local(request)}))
            return
        result = send(sys.argv[1], request)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
    except (Refused, OSError, ValueError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
