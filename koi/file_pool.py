"""Owned, single-file ZFS backing. Existing pools are never adopted or forced."""

import os
from pathlib import Path
import re
import stat
import uuid

from disks import OWNER, STATE_FORMAT, Disks, check_state_format, identifier, save, sync_directory, zfs


def size_bytes(value):
    if not re.fullmatch(r'[1-9][0-9]*GiB', value):
        raise ValueError('Pool size must be an integer GiB value, e.g. 32GiB')
    return int(value[:-3]) * 1024**3


def pools():
    import host
    return host.run('zpool', 'list', '-H', '-o', 'name').splitlines()


def backing(record):
    import host
    check_state_format(record)
    path = Path(record['file'])
    host.secure_directory_chain(path.parent)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('Unsafe pool backing file; retained')
    if info.st_size != record['size']:
        raise ValueError('Pool backing file size changed; retained')
    identity = [info.st_dev, info.st_ino]
    if record.get('identity') is not None and identity != record['identity']:
        raise ValueError('Pool backing file replaced; retained')
    return identity


def check(record):
    import host
    backing(record)
    pool = identifier(record['pool'], r'[a-zA-Z][a-zA-Z0-9_.-]*')
    marker = zfs('get', '-H', '-o', 'value,source', OWNER, pool)
    if marker.split() != [record['owner'], 'local']:
        raise ValueError('Foreign pool ownership; retained: ' + pool)
    status = host.run('zpool', 'status', '-P', pool)
    rows = status.split('config:', 1)[1].split('errors:', 1)[0].splitlines()
    devices = [line.split()[0] for line in rows if len(line.split()) >= 2
               and line.split()[1] in ('ONLINE', 'DEGRADED', 'FAULTED', 'OFFLINE', 'UNAVAIL', 'REMOVED')]
    if devices != [pool, record['file']]:
        raise ValueError('Pool vdev configuration changed; retained')
    guid = host.run('zpool', 'get', '-H', '-o', 'value', 'guid', pool)
    if record.get('guid') is not None and guid != record['guid']:
        raise ValueError('Pool identity changed; retained')
    return guid


def ensure(record):
    import host
    backing(record)
    if record['pool'] not in pools():
        host.run('zpool', 'import', '-N', '-o', 'cachefile=none', '-d', record['file'], record.get('guid') or record['pool'])
    return check(record)


def prepare(state, dataset, path, size):
    import host
    state, path = Path(state), Path(path).absolute()
    host.secure_state_path(state)
    if state.exists() or state.is_symlink():
        info = state.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError('File-backed storage needs root-private state')
    if path != state / 'pool.img':
        raise ValueError('The owned backing file must be STATE/pool.img')
    if any(character.isspace() for character in str(path)):
        raise ValueError('Pool backing path must not contain whitespace')
    if len(dataset.split('/')) != 2:
        raise ValueError('File-backed storage requires POOL/NAMESPACE')
    pool = identifier(dataset.split('/')[0], r'[a-zA-Z][a-zA-Z0-9_.-]*')
    amount = size_bytes(size)
    receipt = state / 'file-pool.json'
    if (state / 'storage.json').exists():
        Disks(state)
    if not receipt.exists():
        if pool in pools() or path.exists() or path.is_symlink():
            raise ValueError('Refusing an existing pool or backing file without an ownership receipt')
        if state.exists() and any(state.iterdir()):
            raise ValueError('Refusing nonempty state without a file-pool receipt')
        state.mkdir(mode=0o700, exist_ok=True)
        info = state.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError('File-backed storage needs root-private state')
        record = {'stateFormat': STATE_FORMAT, 'pool': pool, 'dataset': dataset, 'file': str(path), 'size': amount,
                  'owner': uuid.uuid4().hex, 'identity': None, 'guid': None, 'phase': 'creating'}
        save(receipt, record)
    record = host.private(receipt)
    check_state_format(record)
    record.setdefault('stateFormat', STATE_FORMAT)
    if (record['dataset'], record['file'], record['size']) != (dataset, str(path), amount):
        raise ValueError('Retained file-pool settings differ')
    if record['phase'] != 'creating':
        if record['phase'] != 'ready':
            raise ValueError('Finish file-pool purge before reinstalling')
        ensure(record)
        record['stateFormat'] = STATE_FORMAT
        save(receipt, record)
        return record
    if not path.exists():
        with path.open('xb') as stream:
            os.chmod(path, 0o600)
            stream.truncate(amount)                                                          
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(state)
    record['identity'] = backing(record)
    save(receipt, record)
    if pool not in pools():
        result = host.run('zpool', 'import', '-d', str(path))
        if re.search(r'^\s*pool:\s*' + re.escape(pool) + r'\s*$', result, re.M):
            ensure(record)
        else:
            host.run('zpool', 'create', '-o', 'cachefile=none', '-O', 'mountpoint=none',
                     '-O', 'canmount=off', '-O', OWNER + '=' + record['owner'], pool, str(path))
    record['guid'] = check(record)
    record['phase'] = 'ready'
    save(receipt, record)
    return record


def check_removal(record):
    ensure(record)
    names = zfs('list', '-H', '-o', 'name', '-t', 'filesystem,volume,snapshot', '-r', record['pool']).splitlines()
    if any(name != record['pool'] and name != record['dataset'] and
           not name.startswith(record['dataset'] + '/') for name in names):
        raise ValueError('Foreign pool contents; retained')


def destroy(record, persist):
    """Called only after namespace deletion; persist the destruction intent."""
    import host
    check_state_format(record)
    path = Path(record['file'])
    if path.exists() or path.is_symlink():
        backing(record)
    elif record['pool'] in pools():
        raise ValueError('Live pool backing file missing; retained')
    if record['phase'] == 'destroyed':
        if record['pool'] in pools():
            raise ValueError('Pool name reused after destruction; retained')
        return
    if record['phase'] != 'destroying':
        ensure(record)
        names = zfs('list', '-H', '-o', 'name', '-t', 'filesystem,volume,snapshot', '-r', record['pool']).splitlines()
        if names != [record['pool']]:
            raise ValueError('Pool is not empty; retained')
        record['phase'] = 'destroying'
        persist(record)
    if record['pool'] in pools():
        check(record)
        if zfs('list', '-H', '-o', 'name', '-t', 'filesystem,volume,snapshot', '-r', record['pool']).splitlines() != [record['pool']]:
            raise ValueError('Pool is not empty; retained')
        host.run('zpool', 'destroy', record['pool'])
    record['phase'] = 'destroyed'
    persist(record)


def purge_partial(state):
    """Recover a failure before host integration was published; no VM disks allowed."""
    import host
    state = Path(state)
    record = host.private(state / 'file-pool.json')
    check_state_format(record)
    if (state / 'storage.json').exists():
        Disks(state)
    if not Path(record['file']).exists() and record['pool'] not in pools():
        return
    if record['phase'] not in ('destroying', 'destroyed'):
        if record['phase'] == 'creating':
            record = prepare(state, record['dataset'], Path(record['file']), str(record['size'] // 1024**3) + 'GiB')
        check_removal(record)
        names = zfs('list', '-H', '-o', 'name', '-t', 'filesystem,volume,snapshot', '-r', record['pool']).splitlines()
        expected = {record['pool'], record['dataset'], record['dataset'] + '/images', record['dataset'] + '/machines'}
        if set(names) - expected:
            raise ValueError('Partial setup contains disks or foreign resources; retained')
        store = Disks(state) if (state / 'storage.json').exists() else None
        for name in sorted(set(names) - {record['pool']}, key=len, reverse=True):
            if store is None:
                raise ValueError('Missing namespace ownership records; retained')
            store.owned(name, 'filesystem')
            zfs('destroy', name)
    destroy(record, lambda value: save(state / 'file-pool.json', value))
