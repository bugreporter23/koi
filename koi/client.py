"""Small local frontend for the host-installed Koi backend."""

import argparse
import os
from pathlib import Path
import stat
import subprocess
import sys

HOST = '/etc/koi/bin/koi-host'
HELPER = '/etc/koi/bin/koi-root'
LOGIN = '/etc/koi/bin/koi-login'
CLIENT_ID = Path('/etc/koi/client-id')
ADMIN = ('install', 'uninstall', 'image')


def sudo_executable():
    for candidate in ('/run/wrappers/bin/sudo', '/usr/bin/sudo', '/bin/sudo'):
        path = Path(candidate)
        if path.exists():
            info = path.stat()
            if info.st_uid == 0 and info.st_mode & stat.S_ISUID:
                return str(path)
    raise ValueError('Host has no root-owned setuid sudo command')


def main(client_id, version):
    parser = argparse.ArgumentParser(prog='koi', description=__doc__)
    parser.add_argument('--version', action='version', version='koi ' + version)
    if sys.argv[1:] == ['--version']:
        parser.parse_args()
    try:
        if not CLIENT_ID.exists():
            raise ValueError('Enable services.koi and rebuild the host first')
        if CLIENT_ID.read_text().strip() != client_id:
            raise ValueError('User tools do not match the installed Koi host; use /run/current-system/sw/bin/koi '
                             'or install koi-user-tools from the same revision as the host')
        command = sys.argv[1:] or ['--help']
        if command[0] == 'login':
            login = argparse.ArgumentParser(prog='koi login')
            login.add_argument('adapter', choices=('codex', 'claude'))
            login.add_argument('options', nargs=argparse.REMAINDER)
            selected = login.parse_args(command[1:])
            subprocess.run([LOGIN, selected.adapter, *selected.options], check=True)
            command = ['import-login', selected.adapter]
            if os.geteuid() == 0:
                command.append('--root-login')
        request = ['--client', client_id, *command]
        if os.geteuid() == 0 or command[0] in ('--help', '-h'):
            os.execv(HOST, [HOST, *request])
        sudo = sudo_executable()
        if command[0] in ADMIN:
            os.execv(sudo, [sudo, '--', HOST, *request])
        os.execv(sudo, [sudo, '-n', '--', HELPER, *request])
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, 'koi: ' + str(error) + '\n')
