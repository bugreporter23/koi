"""Host-only SSH CA and client key; five-minute, VM-scoped user certificates.

Reuses Koi's principal, validity and permit-pty policy. Neither private key is
mounted into a VMM or copied into a seed. Guest host-key pinning is independent.
"""

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time

from disks import atomic, identifier, sync_directory
import seed


def paths(state):
    directory = Path(state) / 'ssh-access'
    return {'directory': directory, 'ca': directory / 'ca', 'client': directory / 'client'}


def private(path, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Unsafe SSH authority state: ' + str(path))


def check(state):
    values = paths(state)
    private(Path(state), directory=True)
    private(values['directory'], directory=True)
    for name in ('ca', 'client'):
        private(values[name])
        private(values[name].with_suffix('.pub'))
    return values


def initialize(state):
    values = paths(state)
    private(Path(state), directory=True)
    directory = values['directory']
    directory.mkdir(mode=0o700, exist_ok=True)
    private(directory, directory=True)
    for name in ('ca', 'client'):
        key = values[name]
        public = key.with_suffix('.pub')
        if not key.exists() and not key.is_symlink():
            if public.exists() or public.is_symlink():
                raise ValueError('Incomplete SSH authority retained: ' + name)
            with tempfile.TemporaryDirectory(dir=directory) as temporary:
                generated = Path(temporary) / 'key'
                subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', '', '-f', str(generated)],
                               check=True, capture_output=True, timeout=10)
                atomic(key, generated.read_bytes())
                atomic(public, generated.with_suffix('.pub').read_bytes())
        private(key)
        result = subprocess.check_output(['ssh-keygen', '-y', '-f', str(key)], text=True, timeout=10).strip()
        clean = seed.validate({'name': 'authority', 'id': '0' * 32, 'ssh_key': result})['ssh_key']
        if public.exists() or public.is_symlink():
            private(public)
            if public.read_text().strip() != clean:
                raise ValueError('SSH authority public/private key mismatch')
        else:
            atomic(public, (clean + '\n').encode())
    sync_directory(directory)
    return check(state)


def issue(state, machine, destination):
    identifier(machine, r'[0-9a-f]{32}')
    values = check(state)
    now = int(time.time())
    date = lambda timestamp: time.strftime('%Y%m%d%H%M%SZ', time.gmtime(timestamp))
    with tempfile.TemporaryDirectory(dir=values['directory']) as temporary:
        public = Path(temporary) / 'client.pub'
        public.write_bytes(values['client'].with_suffix('.pub').read_bytes())
        subprocess.run(['ssh-keygen', '-q', '-s', str(values['ca']), '-I', 'koi-' + machine,
                        '-n', 'koi-' + machine, '-V', date(now - 30) + ':' + date(now + 300),
                        '-O', 'clear', '-O', 'permit-pty', str(public)],
                       check=True, capture_output=True, timeout=10)
        atomic(Path(destination), public.with_name('client-cert.pub').read_bytes())
