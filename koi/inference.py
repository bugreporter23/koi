"""Owned inference accounts, per-VM sockets and bounded login import."""

import grp
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys

import host
import isolation
from disks import identifier, save
from login import login_state
from inference_adapters import codex, claude
from inference_adapters.common import token

RUN = Path('/run/koi-inference')
UNITS = Path('/run/systemd/system')
ADAPTERS = {'codex': codex, 'claude': claude}


def credentials(config):
    return Path(config['state']) / 'inference'


def save_host(config):
    save(Path(config['state']) / 'host.json', config)


def check_identity(config):
    identity = config.get('inferenceIdentity')
    if identity is None:
        return None, None
    name = 'tkr' + identifier(config['owner'], r'[0-9a-f]{32}')[:12]
    if identity['account'] != name:
        raise ValueError('Changed inference account binding')
    try:
        account = pwd.getpwnam(name)
    except KeyError:
        account = None
    try:
        group = grp.getgrnam(name)
    except KeyError:
        group = None
    if group and (group.gr_gid == 0 or group.gr_mem or
                  (identity.get('gid') != group.gr_gid and not identity.get('groupPending')) or
                  any(user.pw_gid == group.gr_gid and user.pw_name != name for user in pwd.getpwall())):
        raise ValueError('Refusing foreign inference group')
    if account and (account.pw_uid == 0 or account.pw_gid == 0 or
                    account.pw_gecos != 'koi-inference-' + config['owner'] or
                    account.pw_dir != '/var/empty' or account.pw_shell != '/bin/false' or
                    account.pw_gid != identity.get('gid') or
                    (identity.get('uid') is not None and account.pw_uid != identity['uid'])):
        raise ValueError('Refusing foreign inference account')
    return account, group


def credential_tree(config, uid, gid):
    directory = credentials(config)
    directory.mkdir(mode=0o700, exist_ok=True)
    descriptors = []
    try:
        root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append((root, True))
        if set(os.listdir(root)) - {'codex', 'claude-token', 'refresh.lock'}:
            raise ValueError('Unexpected inference state retained')
        if 'codex' not in os.listdir(root):
            os.mkdir('codex', mode=0o700, dir_fd=root)
        codex_dir = os.open('codex', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        descriptors.append((codex_dir, True))
        if set(os.listdir(codex_dir)) - {'auth.json'}:
            raise ValueError('Unexpected Codex credential state retained')
        for parent, filename in ((root, 'claude-token'), (root, 'refresh.lock'), (codex_dir, 'auth.json')):
            try:
                fd = os.open(filename, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                continue
            descriptors.append((fd, False))
        for fd, is_directory in descriptors:
            info = os.fstat(fd)
            if (not (stat.S_ISDIR(info.st_mode) if is_directory else stat.S_ISREG(info.st_mode))
                    or info.st_uid not in (0, config['inferenceIdentity'].get('uid'))):
                raise ValueError('Unsafe inference credential state retained')
        for fd, is_directory in descriptors:
            os.fchmod(fd, 0o700 if is_directory else 0o600)
            os.fchown(fd, uid, gid)
    finally:
        for fd, _ in reversed(descriptors):
            os.close(fd)


def prepare_identity(config):
    if 'inferenceIdentity' not in config:
        config['inferenceIdentity'] = {'account': 'tkr' + config['owner'][:12], 'uid': None, 'gid': None}
        save_host(config)
    identity = config['inferenceIdentity']
    account, group = check_identity(config)
    if account is None:
        if group is None:
            identity['groupPending'] = True
            save_host(config)
            host.run('groupadd', '--system', identity['account'])
            _, group = check_identity(config)
        identity['gid'] = group.gr_gid
        identity['groupPending'] = False
        identity['uid'] = None
        save_host(config)
        host.run('useradd', '--system', '--no-user-group', '--gid', str(group.gr_gid),
                 '--no-create-home', '--home-dir', '/var/empty', '--shell', '/bin/false',
                 '--comment', 'koi-inference-' + config['owner'], identity['account'])
        account, _ = check_identity(config)
    credential_tree(config, account.pw_uid, account.pw_gid)
    identity['uid'], identity['gid'] = account.pw_uid, account.pw_gid
    save_host(config)


def retire_identity(config):
    if 'inferenceIdentity' not in config:
        return
    account, group = check_identity(config)
    credential_tree(config, 0, 0)
    if account:
        host.run('userdel', account.pw_name)
    if group:
        host.run('groupdel', group.gr_name)


def socket_directory(record):
    return RUN / identifier(record['id'], r'[0-9a-f]{32}')


def units(record):
    name = 'koi-inference-' + identifier(record['id'], r'[0-9a-f]{32}')
    return name + '.socket', name + '.service'


def texts(config, record, binding):
    socket_unit, service_unit = units(record)
    source = Path(__file__).resolve().parent
    worker = 'koi-worker-' + record['id']
    private_credentials = '/run/' + worker + '/credentials'
    snippet = 'import sys; sys.path.insert(0, ' + repr(str(source)) + '); import relay; relay.main()'
    command = ' '.join(isolation.quoted(value) for value in
                       (sys.executable, '-I', '-c', snippet, '--credentials', private_credentials))
    account = identifier(config['inferenceIdentity']['account'], r'tkr[0-9a-f]{12}')
    group = identifier(binding['account'], r'tkv[0-9a-f]{12}')
    return {
        socket_unit: ('[Unit]\nDescription=Koi VM inference capability\n'
                      '[Socket]\nListenStream=' + str(socket_directory(record) / 'relay.sock') + '\n'
                      'SocketUser=root\nSocketGroup=' + group + '\nSocketMode=0660\n'
                      'DirectoryMode=0755\nRemoveOnStop=yes\nService=' + service_unit + '\n'),
        service_unit: ('[Unit]\nDescription=Koi unprivileged inference relay\nRequires=' + socket_unit + '\n'
                       'After=' + socket_unit + '\n'
                       '[Service]\nUser=' + account + '\nGroup=' + account + '\n'
                       'ExecStart=' + command + '\n'
                       'RuntimeDirectory=' + worker + '\nRuntimeDirectoryMode=0700\n'
                       'BindPaths=' + isolation.bind_spec(str(credentials(config)) + ':' + private_credentials) + '\n'
                       'ReadWritePaths=' + private_credentials + '\nProtectSystem=strict\nProtectHome=yes\n'
                       'PrivateTmp=yes\nPrivateDevices=yes\nNoNewPrivileges=yes\n'
                       'CapabilityBoundingSet=\nRestrictSUIDSGID=yes\nLockPersonality=yes\n'
                       'ProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\n'
                       'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\nUMask=0077\n'
                       'KillMode=control-group\nTimeoutStopSec=5\n'),
    }


def ensure(config, record, binding):
    if binding.get('inferenceRelay') != 1:
        return
    if config['status'] != 'installed':
        raise ValueError('Finish koi install before granting inference')
    if check_identity(config)[0] is None:
        raise ValueError('Inference account is missing; rerun koi install')
    check_units(config, record)
    desired = texts(config, record, binding)
    changed = False
    for name, text in desired.items():
        path = UNITS / name
        if not path.exists() or path.read_text() != text:
            host.write_file(config, path, text)
            changed = True
    host.parents(config, socket_directory(record) / 'relay.sock')
    host.secure_directory_chain(socket_directory(record))
    if changed:
        host.run('systemctl', 'daemon-reload')
    if binding.get('inferenceGranted', False):
        host.run('systemctl', 'start', units(record)[0])
    else:
        stop(config, record)


def check_units(config, record):
    for name in units(record):
        path = UNITS / name
        if path.exists() or path.is_symlink():
            if host.fingerprint(path) not in (config['files'].get(str(path)), config.get('previousFiles', {}).get(str(path))):
                raise ValueError('Changed inference unit retained: ' + name)


def stop(config, record):
    check_units(config, record)
    present = []
    for name in units(record):
        path = UNITS / name
        if path.exists():
            present.append(name)
        elif str(path) in config['files']:
            if host.run('systemctl', 'show', name, '--property=LoadState', '--value') != 'not-found':
                present.append(name)
    if present:
        host.run('systemctl', 'stop', *present)


def remove(config, record):
    stop(config, record)
    for name in units(record):
        (UNITS / name).unlink(missing_ok=True)


def set_grant(config, name, granted):
    import runtime
    controller = runtime.Runtime(config['state'])
    with controller.store.locked():
        record = controller.machine(name)
        binding = controller.configuration(record)
        if binding.get('inferenceRelay') != 1:
            raise ValueError('This retained disk lacks the guest inference forwarder; create a VM from a relay-capable image')
        check_units(config, record)
        live = not runtime.stopped(controller.unit(record))
        if live:
            controller.check_unit(record, binding, live=True)
        binding['inferenceGranted'] = granted
        save(controller.directory(record) / 'config.json', binding)
        if granted and live:
            ensure(config, record, binding)
        else:
            stop(config, record)
    print(name + (' inference granted' if granted else ' inference revoked'))


def import_login(config, adapter, *, root_login=False):
    if root_login and os.geteuid() != 0:
        raise ValueError('Root login import requires root')
    if config['status'] != 'installed':
        raise ValueError('Finish koi install before importing a login')
    account, _ = check_identity(config)
    if account is None:
        raise ValueError('Inference account is missing; rerun koi install')
    operator = pwd.getpwuid(0) if root_login else pwd.getpwnam(config['operator'])
    relative = 'codex/auth.json' if adapter == 'codex' else 'claude-token'
    source = login_state(operator.pw_dir) / relative
    snippet = ('import os, stat, sys; fd=os.open(sys.argv[1], os.O_RDONLY|os.O_NOFOLLOW); '
               's=os.fstat(fd); assert stat.S_ISREG(s.st_mode) and s.st_uid==os.geteuid() and not s.st_mode & 0o077; '
               'b=os.read(fd,65537); assert len(b)<=65536; sys.stdout.buffer.write(b)')
    result = subprocess.run([sys.executable, '-I', '-c', snippet, str(source)],
                            user=operator.pw_uid, group=operator.pw_gid, extra_groups=[],
                            env={'PATH': '/usr/bin:/bin'}, capture_output=True, timeout=10)
    if result.returncode:
        raise ValueError('Cannot read private operator login; run koi login ' + adapter)
    if adapter == 'codex':
        value = codex.validate(json.loads(result.stdout))
        data = json.dumps({'tokens': value['tokens'], 'last_refresh': value.get('last_refresh')}) + '\n'
    else:
        data = token(result.stdout.decode().strip()) + '\n'
    directory = credentials(config)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != account.pw_uid or info.st_mode & 0o077:
            raise ValueError('Unsafe inference credential directory')
        source = Path(__file__).resolve().parent
        snippet = ('import sys\nfrom pathlib import Path\nsys.path.insert(0,' + repr(str(source)) + ')\n'
                   'from inference_adapters.common import locked,write_private\n'
                   'state=Path("/proc/self/fd/"+sys.argv[1])\n'
                   'data=sys.stdin.read(65537)\n'
                   'with locked(state):\n    write_private(state/sys.argv[2],data)\n')
        result = subprocess.run([sys.executable, '-I', '-c', snippet, str(fd), relative],
                                input=data.encode(), user=account.pw_uid, group=account.pw_gid,
                                extra_groups=[], pass_fds=(fd,), env={'PATH': '/usr/bin:/bin'},
                                capture_output=True, timeout=10)
        if result.returncode:
            raise ValueError('Could not import host login into private relay state')
    finally:
        os.close(fd)
    print('Host ' + adapter + ' login imported for VM inference')
