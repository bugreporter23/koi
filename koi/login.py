"""Native provider login, run as the caller using host-pinned tools."""

import argparse
import os
from pathlib import Path
import pwd
import subprocess

from inference_adapters import codex, claude


def login_state(home):
    return Path(home) / '.local/state/koi-vm'


def login(adapter, options=()):
    state = login_state(pwd.getpwuid(os.geteuid()).pw_dir)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.umask(0o077)
    if options[:1] == ['--']:
        options = options[1:]
    {'codex': codex, 'claude': claude}[adapter].login(state, options)


def main():
    parser = argparse.ArgumentParser(prog='koi login', description=__doc__)
    parser.add_argument('adapter', choices=('codex', 'claude'))
    parser.add_argument('options', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        login(args.adapter, args.options)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
        parser.exit(1, 'koi login failed; check the host login and installation\n')
