"""Root-only boot projection. Incus retains the authoritative inventory."""

import os
import json
import argparse
import sys

from . import network
from .common import atomic, config, require
from .gate import STATE, Gate, operation_lock


def main():
    require(os.geteuid() == 0, "root required")
    if sys.argv[1:2] == ["initialize"]:
        from .provision import initialize

        parser = argparse.ArgumentParser(prog="koi-host initialize")
        parser.add_argument("--storage-zfs", required=True)
        args = parser.parse_args(sys.argv[2:])
        with operation_lock(STATE):
            initialize(args.storage_zfs)
        return
    if sys.argv[1:] == ["select-prefix"]:
        print(network.select_local_prefix())
        return
    if sys.argv[1:2] == ["admin-targets"]:
        from .targets import install, withdraw

        if sys.argv[2:3] == ["withdraw"]:
            parser = argparse.ArgumentParser(prog="koi-host admin-targets withdraw")
            parser.add_argument("--identity-directory", required=True)
            args = parser.parse_args(sys.argv[3:])
            with operation_lock(STATE):
                result = withdraw(args.identity_directory)
            print(json.dumps(result))
            return

        require(
            sys.argv[2:] in (["apply"], ["check"]),
            "expected admin-targets apply or check",
        )
        payload = sys.stdin.buffer.read(65537)
        require(len(payload) <= 65536, "admin targets too large")
        with operation_lock(STATE):
            result = install(json.loads(payload), check=sys.argv[2] == "check")
        print(json.dumps(result))
        return
    if sys.argv[1:] == ["withdraw"]:
        with operation_lock(STATE):
            network.local_hosts(None, [])
        return
    c = config()
    with operation_lock(STATE):
        if sys.argv[1:] == ["pause"]:
            require(
                all(g["status"] == "Stopped" for g in Gate(c).guests()),
                "stop guests explicitly before pausing management",
            )
            atomic(STATE / "maintenance", "operator maintenance\n")
            return
        if sys.argv[1:] == ["resume"]:
            (STATE / "maintenance").unlink(missing_ok=True)
            return
        require(sys.argv[1:] == ["reconcile"], "expected reconcile")
        Gate(c).reconcile()


if __name__ == "__main__":
    main()
