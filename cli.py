"""Koi's control-plane and workstation commands."""

import subprocess
import sys

from koi import client
from koi.common import Refused

import workstation


def main():
    parser = client.parser(workstation.add_commands)
    client.main(parser)


if __name__ == "__main__":
    try:
        main()
    except (
        Refused,
        OSError,
        ValueError,
        KeyError,
        subprocess.CalledProcessError,
    ) as error:
        print("koi: " + str(error), file=sys.stderr)
        sys.exit(1)
