"""Owned stateful setup on a NixOS host. Existing pools and unrelated host resources stay put.

The NixOS module owns the command, root helper, sudo rule, settings and the
network unit. Installation creates only state: storage, image, identities and
the DNS unit, which follows retained VM state and lives in /run."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import stat
import subprocess

from disks import Disks, atomic, identifier, save, zfs

BINDING = Path('/etc/koi/installation.json')
SETTINGS = Path('/etc/koi/settings.json')
UNITS = Path('/run/systemd/system')
NETWORK_UNIT = 'koi.service'
FORWARD = Path('/proc/sys/net/ipv4/ip_forward')
STORE = Path('/nix/store')


def run(*argv, **kwargs):
    result = subprocess.run(argv, text=True, capture_output=True,
                            timeout=kwargs.pop('timeout', 120), **kwargs)
    if result.returncode:
        raise RuntimeError('Command failed: ' + shlex.join(argv) + '\n' + result.stderr.strip())
    return result.stdout.strip()


@contextmanager
def operation_lock():
    directory = Path('/run/koi-controller')
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('Unsafe controller lock directory')
    fd = os.open(directory / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def private(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('Host binding must be a root-owned private regular file: ' + str(path))
    return json.loads(path.read_text())


def load(state=None):
    state = Path(private(BINDING)['state']) if state is None else Path(state)
    config = private(state / 'host.json')
    store = Disks(state)
    if config['owner'] != store.owner or config['dataset'] != store.dataset:
        raise ValueError('Host installation does not match storage ownership')
    return config


def fingerprint(path):
    if path.is_symlink():
        return 'link:' + os.readlink(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
        raise ValueError('Refusing foreign installation file: ' + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_files(config):
    for filename in config.get('directories', []):
        path = Path(filename)
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('Changed installation directory; retained: ' + filename)
    for filename, expected in config['files'].items():
        path = Path(filename)
        if path.exists() or path.is_symlink():
            if fingerprint(path) not in (expected, config.get('previousFiles', {}).get(filename)):
                raise ValueError('Installation file changed; retained: ' + filename)


def parents(config, path):
    missing = []
    ancestor = path.parent
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    secure_directory_chain(ancestor)
    for directory in reversed(missing):
        config.setdefault('directories', []).append(str(directory))
        save(Path(config['state']) / 'host.json', config)
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)


def write_file(config, path, text, mode=0o644):
    expected = hashlib.sha256(text.encode()).hexdigest()
    if path.exists() or path.is_symlink():
        actual = fingerprint(path)
        if actual not in (config['files'].get(str(path)), config.get('previousFiles', {}).get(str(path))):
            raise ValueError('Refusing existing installation file: ' + str(path))
    parents(config, path)
    config.setdefault('previousFiles', {})[str(path)] = config['files'].get(str(path))
    config['files'][str(path)] = expected
    save(Path(config['state']) / 'host.json', config)
    atomic(path, text.encode())
    path.chmod(mode)
    config['previousFiles'].pop(str(path), None)
    save(Path(config['state']) / 'host.json', config)


def write_link(config, path, target):
    expected = 'link:' + str(target)
    if path.exists() or path.is_symlink():
        if fingerprint(path) not in (config['files'].get(str(path)), config.get('previousFiles', {}).get(str(path))):
            raise ValueError('Refusing existing command or GC root: ' + str(path))
    parents(config, path)
    config.setdefault('previousFiles', {})[str(path)] = config['files'].get(str(path))
    config['files'][str(path)] = expected
    save(Path(config['state']) / 'host.json', config)
    temporary = path.with_name(path.name + '.koi-' + config['owner'][:8])
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, path)
    config['previousFiles'].pop(str(path), None)
    save(Path(config['state']) / 'host.json', config)


def links():
    return json.loads(run('ip', '-j', '-d', 'link', 'show'))


def bridge(config):
    matches = [link for link in links() if link['ifname'] == config['bridge']]
    if not matches:
        return None
    value = matches[0]
    pending = (config.get('bridgePending') == 'create' and not value.get('ifalias') and
               value.get('address') == config.get('bridgeMac'))
    if ((value.get('ifalias') != 'koi:' + config['owner'] and not pending) or
            value.get('linkinfo', {}).get('info_kind') != 'bridge'):
        raise ValueError('Refusing foreign bridge: ' + config['bridge'])
    return value


def prefix_available(subnet, own_bridge=None):
    subnet = ipaddress.IPv4Network(subnet)
    for route in json.loads(run('ip', '-j', 'route', 'show', 'table', 'all')):
        destination = route.get('dst', 'default')
        if destination == 'default' or ':' in destination or route.get('dev') == own_bridge:
            continue
        if subnet.overlaps(ipaddress.IPv4Network(destination, strict=False)):
            raise ValueError('Guest subnet overlaps host route: ' + destination)


def firewall_rules(config):
    bridge_name, prefix, subnet = config['bridge'], config['prefix'], config['subnet']
    marker = ['-m', 'comment', '--comment', 'koi:' + config['owner']]
    return [
        ('iptables', 'filter', 'INPUT', prefix + 'I', ['-i', bridge_name], [
            ['-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED', '-j', 'ACCEPT'],
            ['-p', 'udp', '-m', 'multiport', '--dports', '53,67', '-j', 'ACCEPT'],
            ['-p', 'tcp', '--dport', '53', '-j', 'ACCEPT'],
            ['-p', 'icmp', '-j', 'ACCEPT'], marker + ['-j', 'DROP']]),
        ('iptables', 'filter', 'FORWARD', prefix + 'F', [], [
            ['-i', bridge_name, '-s', subnet, '-j', 'ACCEPT'],
            ['-i', bridge_name, '-j', 'DROP'],
            ['-o', bridge_name, '-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED', '-j', 'ACCEPT'],
            ['-i', 'tailscale0', '-o', bridge_name, '-d', subnet, '-j', 'ACCEPT'],
            ['-o', bridge_name, '-j', 'DROP'], marker + ['-j', 'RETURN']]),
        ('iptables', 'nat', 'POSTROUTING', prefix + 'N', ['-s', subnet], [
            ['!', '-o', bridge_name, '-j', 'MASQUERADE'], marker + ['-j', 'RETURN']]),
        ('ip6tables', 'filter', 'INPUT', prefix + '6I', ['-i', bridge_name], [marker + ['-j', 'DROP']]),
        ('ip6tables', 'filter', 'FORWARD', prefix + '6F', [], [
            ['-i', bridge_name, '-j', 'DROP'], ['-o', bridge_name, '-j', 'DROP'], marker + ['-j', 'RETURN']]),
    ]


def iptables(config, command, table, *args, check=True):
    result = subprocess.run([config['tools'][command], '-w', '5', '-t', table, *args],
                            text=True, capture_output=True, timeout=30)
    if check:
        result.check_returncode()
    return result


def chain_exists(config, command, table, chain):
    result = iptables(config, command, table, '-S', chain, check=False)
    if result.returncode == 0:
        return result
    if result.returncode == 1 and any(value in result.stderr for value in
                                    ('No chain/target/match', 'does not exist')):
        return None
    raise RuntimeError('Cannot inspect firewall chain: ' + chain + '\n' + result.stderr.strip())


def check_firewall(config):
    for command, table, parent, chain, match, rules in firewall_rules(config):
        value = chain_exists(config, command, table, chain)
        if value is not None:
            lines = value.stdout.strip().splitlines()
            if len(lines) > len(rules) + 1:
                raise ValueError('Foreign rules in koi chain: ' + chain)
            known = sum(iptables(config, command, table, '-C', chain, *rule,
                                 check=False).returncode == 0 for rule in rules)
            if known != len(lines) - 1:
                raise ValueError('Changed koi firewall chain: ' + chain)


def network_up(config):
    if config.get('filePool'):
        import file_pool
        file_pool.ensure(config['filePool'])
    bridge(config)
    prefix_available(config['subnet'], config['bridge'])
    check_firewall(config)
    name = config['bridge']
    if bridge(config) is None:
        config['bridgePending'] = 'create'
        save(Path(config['state']) / 'host.json', config)
        run('ip', 'link', 'add', 'name', name, 'address', config['bridgeMac'], 'type', 'bridge')
    run('ip', 'link', 'set', name, 'alias', 'koi:' + config['owner'])
    config['bridgePending'] = None
    save(Path(config['state']) / 'host.json', config)
    run('ip', 'link', 'set', name, 'addrgenmode', 'none')
    run('ip', 'address', 'replace', config['gateway'] + '/24', 'dev', name)
    run('ip', 'link', 'set', name, 'up')
    for command, table, parent, chain, match, rules in firewall_rules(config):
        if chain_exists(config, command, table, chain) is None:
            iptables(config, command, table, '-N', chain)
        for rule in rules:
            if iptables(config, command, table, '-C', chain, *rule, check=False).returncode:
                iptables(config, command, table, '-A', chain, *rule)
        if iptables(config, command, table, '-C', parent, *match, '-j', chain, check=False).returncode:
            iptables(config, command, table, '-I', parent, '1', *match, '-j', chain)
    FORWARD.write_text('1\n')
    import managed
    for path in (Path(config['state']) / 'runtime').glob('*/managed.json'):
        allocation = private(path)
        managed.prepare_account(config, allocation)
        managed.prepare_link(config, allocation)
    path = UNITS / config['dnsUnit']
    text = dns_service(config)
    if not path.exists() or path.read_text() != text:
        write_file(config, path, text)
        run('systemctl', 'daemon-reload')
    run('systemctl', 'start', '--no-block', config['dnsUnit'])


def network_down(config):
    check_firewall(config)
    bridge_value = bridge(config)
    if bridge_value is not None:
        members = [link for link in links() if link.get('master') == config['bridge']]
        if members:
            raise ValueError('Bridge still has interfaces; retained: ' + config['bridge'])
    for command, table, parent, chain, match, rules in firewall_rules(config):
        if chain_exists(config, command, table, chain) is None:
            continue
        if iptables(config, command, table, '-C', parent, *match, '-j', chain, check=False).returncode == 0:
            iptables(config, command, table, '-D', parent, *match, '-j', chain)
        iptables(config, command, table, '-F', chain)
        iptables(config, command, table, '-X', chain)
    if bridge_value is not None:
        run('ip', 'link', 'delete', config['bridge'])
    if FORWARD.read_text().strip() == '1':
        FORWARD.write_text(config['forwardBefore'] + '\n')


def secure_directory_chain(directory):
    for ancestor in (directory, *directory.parents):
        info = ancestor.lstat()
        writable = info.st_mode & 0o022
        if ancestor == STORE and info.st_mode & stat.S_ISVTX and not info.st_mode & 0o002:
            writable = 0
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or writable:
            raise ValueError('Installation needs existing root-owned, non-writable parent directories: ' + str(ancestor))


def secure_state_path(path):
    if not path.is_absolute():
        raise ValueError('State path must be absolute')
    secure_directory_chain(path.parent)


def packaged_entry(entry):
    """A privileged package must not be replaceable through a writable parent."""
    entry = Path(entry).resolve()
    if not str(entry).startswith('/nix/store/'):
        raise ValueError('Install only the immutable Nix-packaged command')
    secure_directory_chain(entry.parent)
    info = entry.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('Packaged command must be a root-owned, non-writable regular file')
    return entry


def dns_service(config):
    import managed
    token = config['owner'][:8]
    directory = Path(config['hosts']).parent
    legacy = ''
    capabilities = 'CAP_NET_BIND_SERVICE'
    if managed.requires_dhcp(config):
        capabilities += ' CAP_NET_RAW CAP_NET_ADMIN'
        legacy = (' --dhcp-range=' + config['subnet'].split('/')[0] + ',static,255.255.255.0,1h'
                  ' --dhcp-hostsfile=/run/koi-dns-' + token + '/config/hosts'
                  ' --dhcp-leasefile=/run/koi-dns-' + token + '/leases')
    return ('[Unit]\nDescription=Koi guest DNS\nRequires=' + config['networkUnit'] + '\nAfter=' + config['networkUnit'] + '\n'
           '[Service]\nDynamicUser=yes\nRuntimeDirectory=koi-dns-' + token + '\nRuntimeDirectoryPreserve=restart\n'
           'AmbientCapabilities=' + capabilities + '\n'
           'CapabilityBoundingSet=' + capabilities + '\n'
           'NoNewPrivileges=yes\nProtectSystem=strict\nProtectHome=yes\n'
           'BindReadOnlyPaths=' + str(directory) + ':/run/koi-dns-' + token + '/config\n'
           'ExecStart=' + config['dnsmasq'] +
           ' --keep-in-foreground --conf-file=/dev/null --interface=' + config['bridge'] +
           ' --bind-interfaces --except-interface=lo --no-hosts --domain=koi --local=/koi/' +
           ' --addn-hosts=/run/koi-dns-' + token + '/config/dns-hosts' + legacy + '\n'
           '[Install]\nWantedBy=multi-user.target\n')


def refresh_dns(config):
    path = UNITS / config['dnsUnit']
    text = dns_service(config)
    if not path.exists() or path.read_text() != text:
        write_file(config, path, text)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'restart', config['dnsUnit'])
    else:
        run('systemctl', 'kill', '--kill-whom=main', '--signal=HUP', config['dnsUnit'])


def settings():
    """Host settings declared through services.koi; never taken from the command line."""
    if not Path('/etc/NIXOS').exists():
        raise ValueError('koi requires a NixOS host')
    try:
        target = SETTINGS.resolve(strict=True)
    except FileNotFoundError:
        raise ValueError('Enable services.koi in the NixOS configuration and rebuild first') from None
    if not str(target).startswith('/nix/store/'):
        raise ValueError('koi settings must come from the NixOS configuration: ' + str(SETTINGS))
    return json.loads(target.read_text())


def install(args):
    declared = settings()
    if os.uname().machine != 'x86_64' or not Path('/dev/kvm').exists():
        raise ValueError('Install requires x86-64 Linux with working KVM')
    if not Path('/run/systemd/system').is_dir():
        raise ValueError('Install requires the host systemd manager')
    state = Path(declared['state'])
    secure_state_path(state)
    dataset = declared['dataset']
    pool_file = state / 'pool.img' if declared.get('poolSize') else None
    subnet = ipaddress.IPv4Network(declared['subnet'])
    if subnet.prefixlen != 24 or not any(subnet.subnet_of(ipaddress.IPv4Network(value)) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        raise ValueError('Choose an explicit private IPv4 /24')
    operator = pwd.getpwnam(declared['operator'])
    identifier(operator.pw_name, r'[a-zA-Z_][a-zA-Z0-9_-]*')
    entry = packaged_entry(os.environ['KOI_EXECUTABLE'])
    package = entry.parent.parent
    for tool in ['zfs', 'zpool', 'ip', 'iptables', 'ip6tables', 'dnsmasq', 'dhcp_release', 'useradd', 'userdel', 'groupadd', 'groupdel', 'usermod', 'nix-store', 'socat']:
        if shutil.which(tool) is None:
            raise ValueError('Missing installation prerequisite: ' + tool)
    run('systemctl', 'show', '--property=Version')
    config = None
    if BINDING.exists():
        config = load()
        if (config['state'], config['dataset'], config['subnet'], config['operator']) != (
                str(state), dataset, str(subnet), operator.pw_name):
            raise ValueError('Another installation exists; restore its services.koi settings')
        check_files(config)
    else:
        prefix_available(str(subnet))
        if (state / 'host.json').exists():
            config = load(state)
            if (config['dataset'], config['subnet']) != (dataset, str(subnet)) or (config['status'] != 'uninstalled' and config['operator'] != operator.pw_name):
                raise ValueError('Retained install settings differ')
            check_files(config)
    import file_pool
    file_record = None
    if pool_file is not None:
        file_record = file_pool.prepare(state, dataset, pool_file, declared['poolSize'])
    elif (state / 'file-pool.json').exists():
        file_record = private(state / 'file-pool.json')
        if file_record['dataset'] != dataset:
            raise ValueError('Retained file-pool settings differ')
        file_pool.ensure(file_record)
    zfs('get', '-H', '-o', 'value', 'type', dataset.split('/')[0])
    if config is None:
        store = Disks.initialize(state, dataset)
        config = {'state': str(state), 'dataset': dataset, 'owner': store.owner,
                  'subnet': str(subnet), 'gateway': str(subnet.network_address + 1),
                  'bridge': 'tkb' + store.owner[:8], 'prefix': 'TK' + store.owner[:8],
                  'operator': operator.pw_name, 'files': {}, 'image': None,
                  'forwardBefore': FORWARD.read_text().strip(), 'status': 'installing'}
    if file_record is not None:
        config['filePool'] = file_record
    store = Disks(config['state'])
    with store.locked():
        store.owned(store.dataset + '/machines', 'filesystem')
        zfs('set', 'volmode=dev', store.dataset + '/machines')
    import access
    access.initialize(Path(config['state']))
    config['bridgeMac'] = '02:' + ':'.join(config['owner'][i:i+2] for i in range(0, 10, 2))
    config['operator'] = operator.pw_name
    config['status'] = 'installing'
    config['package'] = str(package)
    vmm = Path(shutil.which('cloud-hypervisor')).resolve()
    config['vmm'] = str(vmm)
    config['closure'] = run('nix-store', '-qR', str(vmm.parent.parent)).splitlines()
    config['tools'] = {name: str(Path(shutil.which(name)).parent.resolve() / name) for name in ('iptables', 'ip6tables')}
    token = config['owner'][:8]
    directory = Path(config['state']) / 'dns'
    config['networkUnit'] = NETWORK_UNIT
    config['dnsUnit'] = 'koi-dns-' + token + '.service'
    config['hosts'] = str(directory / 'hosts')
    save(Path(config['state']) / 'host.json', config)
    write_file(config, BINDING, json.dumps({'state': config['state']}) + '\n', 0o600)
    write_link(config, Path('/nix/var/nix/gcroots') / ('koi-' + token), package)
    if args.image is not None:
        config['image'] = Disks(config['state']).import_image(args.image)['id']
    save(Path(config['state']) / 'host.json', config)
    config['dnsmasq'] = str(Path(shutil.which('dnsmasq')).resolve())
    import inference
    inference.prepare_identity(config)
    import managed
    managed.write_hosts(config)
    directory.chmod(0o755)                                                                   
    write_file(config, UNITS / config['dnsUnit'], dns_service(config))
    run('systemctl', 'daemon-reload')
    run('systemctl', 'restart', config['dnsUnit'])
    import managed
    for path in (Path(config['state']) / 'runtime').glob('*/managed.json'):
        allocation = private(path)
        managed.prepare_account(config, allocation)
        managed.prepare_link(config, allocation)
    config['status'] = 'installed'
    save(Path(config['state']) / 'host.json', config)
    print('Installed. Run: koi new scratch')


def finish_purge(receipt):
    owner = identifier(receipt['owner'], r'[0-9a-f]{32}')
    root = Path('/nix/var/nix/gcroots') / ('koi-' + owner[:8])
    if root.exists() or root.is_symlink():
        if fingerprint(root) != 'link:' + receipt['package']:
            raise ValueError('Changed GC root retained during final cleanup')
    state = Path(receipt['state'])
    secure_state_path(state)
    if receipt.get('filePool'):
        import file_pool
        if Path(receipt['filePool']['file']) != state / 'pool.img':
            raise ValueError('File-pool cleanup path differs from owned state')
        def persist(record):
            receipt['filePool'] = record
            save(BINDING, receipt)
        file_pool.destroy(receipt['filePool'], persist)
    if state.exists() or state.is_symlink():
        info = state.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError('Unsafe retained state during final cleanup')
        shutil.rmtree(state)
    root.unlink(missing_ok=True)
    BINDING.unlink()
    print('Removed owned setup and disks. ' + ('Owned file-backed pool removed.' if receipt.get('filePool')
                                             else 'Existing ZFS pool preserved.'))


def uninstall(args):
    if not BINDING.exists():
        if (args.state / 'file-pool.json').exists():
            secure_state_path(args.state)
            secure_directory_chain(args.state)
            if (args.state / 'host.json').exists():
                raise ValueError('Retained installation must be reinstalled before purge')
            if args.purge:
                if not args.yes and input('Type purge to remove the partial file-backed setup: ') != 'purge':
                    raise ValueError('Purge cancelled; nothing removed')
                import file_pool
                file_pool.purge_partial(args.state)
                shutil.rmtree(args.state)
                print('Removed partial file-backed setup.')
            else:
                print('Partial file-backed setup retained; use uninstall --purge to remove it.')
            return
        print('No active koi installation. Retained disks require reinstall before purge.')
        return
    receipt = private(BINDING)
    if receipt.get('phase') == 'purge-state':
        finish_purge(receipt)
        return
    config = load()
    check_files(config)
    check_firewall(config)
    bridge(config)
    import inference
    inference.check_identity(config)
    import managed
    state = Path(config['state'])
    names = [path.stem for path in (state / 'machines').glob('*.json')]
    if args.purge:
        print('Purge deletes VM disks and base images in ' + config['dataset'] + ': ' + ', '.join(names))
        if not args.yes and input('Type purge to delete these disks: ') != 'purge':
            raise ValueError('Purge cancelled; nothing removed')
        managed.check_storage_removal(config)
        if config.get('filePool'):
            import file_pool
            file_pool.check_removal(config['filePool'])
    for path in (state / 'runtime').glob('*/managed.json'):
        managed.check_allocation(config, private(path))
    config['status'] = 'uninstalling'
    save(state / 'host.json', config)
    for name in names:
        managed.remove_machine(config, name, delete_disk=args.purge)
    inference.retire_identity(config)
    if (UNITS / config['dnsUnit']).exists():
        run('systemctl', 'stop', config['dnsUnit'])
    network_down(config)
    if args.purge:
        managed.purge_storage(config)
    deferred = {filename: value for filename, value in config['files'].items()
                if filename == str(BINDING) or '/gcroots/' in filename}
    for filename, expected in config['files'].items():
        if filename in deferred:
            continue
        path = Path(filename)
        if path.exists() or path.is_symlink():
            if fingerprint(path) not in (expected, config.get('previousFiles', {}).get(filename)):
                raise ValueError('Changed file retained during uninstall: ' + filename)
            path.unlink()
    run('systemctl', 'daemon-reload')
    for filename in reversed(config.get('directories', [])):
        path = Path(filename)
        if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
            path.rmdir()
    if args.purge:
        receipt = {'phase': 'purge-state', 'state': str(state),
                   'owner': config['owner'], 'package': config['package']}
        if config.get('filePool'):
            receipt['filePool'] = config['filePool']
        save(BINDING, receipt)
        finish_purge(private(BINDING))
    else:
        for filename, expected in sorted(deferred.items(), key=lambda item: item[0] == str(BINDING)):
            path = Path(filename)
            if path.exists() or path.is_symlink():
                if fingerprint(path) != expected:
                    raise ValueError('Changed final installation file retained: ' + filename)
                path.unlink()
        config['files'] = {}
        config['previousFiles'] = {}
        config['directories'] = []
        config['status'] = 'uninstalled'
        save(state / 'host.json', config)
        print('Removed host setup; VM disks, keys and records retained in ' + str(state))


def import_image(source):
    config = load()
    config['image'] = Disks(config['state']).import_image(source)['id']
    save(Path(config['state']) / 'host.json', config)
    refresh_dns(config)
    print('Default image: ' + config['image'])


def parser(commands):
    install_parser = commands.add_parser('install', help='Create state for the services.koi settings')
    install_parser.add_argument('--image', type=Path, help='Import a finished image; never builds or executes image tooling')
    image_parser = commands.add_parser('image', help='Manage finished VM images')
    image_commands = image_parser.add_subparsers(dest='image_operation', required=True)
    image_import = image_commands.add_parser('import', help='Import a finished image for new VMs')
    image_import.add_argument('directory', type=Path)
    uninstall_parser = commands.add_parser('uninstall', help='Remove host setup; keep disks unless --purge')
    uninstall_parser.add_argument('--purge', action='store_true')
    uninstall_parser.add_argument('--yes', action='store_true', help='Explicitly confirm a requested purge')
    uninstall_parser.add_argument('--state', type=Path, default=argparse.SUPPRESS,
                                  help='Private state for recovery before the install binding exists')
