"""Fixed managed VM provisioning and deletion; no general privileged runner."""

import ipaddress
import grp
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import time
from types import SimpleNamespace

import host
from disks import Disks, OWNER, identifier, save, zfs
import trial


def allocations(config):
    return [host.private(path) for path in (Path(config['state']) / 'runtime').glob('*/managed.json')]


def leases(config):
    path = Path('/run') / ('koi-dns-' + config['owner'][:8]) / 'leases'
    if not path.exists():
        return []
    result = []
    network = ipaddress.IPv4Network(config['subnet'])
    for line in path.read_text().splitlines():
        expiry, mac, address, name, client = line.split()
        identifier(mac, r'[0-9a-f]{2}(:[0-9a-f]{2}){5}')
        identifier(client, r'-|[0-9a-f]{2}(:[0-9a-f]{2})*')
        if ipaddress.IPv4Address(address) not in network:
            raise ValueError('DHCP lease outside the owned guest subnet')
        if int(expiry) == 0 or int(expiry) > time.time():
            result.append({'mac': mac, 'address': address, 'client': client})
    return result


def release_lease(config, allocation):
    if allocation.get('network') == 'static-v1':
        return
    for value in leases(config):
        if value['address'] == allocation['address'] and value['mac'] == allocation['mac']:
            args = ['dhcp_release', config['bridge'], value['address'], value['mac']]
            if value['client'] != '-':
                args.append(value['client'])
            host.run(*args)


def check_group(allocation):
    try:
        group = grp.getgrnam(allocation['account'])
    except KeyError:
        return None
    if (group.gr_gid == 0 or group.gr_mem or
            (group.gr_gid != allocation.get('gid') and allocation.get('groupPending') != 'create') or
            any(user.pw_gid == group.gr_gid and user.pw_name != allocation['account'] for user in pwd.getpwall())):
        raise ValueError('Refusing foreign VMM group: ' + allocation['account'])
    return group


def check_account(config, allocation):
    try:
        account = pwd.getpwnam(allocation['account'])
    except KeyError:
        return None
    if (account.pw_uid == 0 or account.pw_gid == 0 or
            account.pw_gecos != 'koi-' + config['owner'] + '-' + allocation['id'] or
            (allocation.get('gid') is not None and account.pw_gid != allocation['gid']) or
            account.pw_dir != '/var/empty' or account.pw_shell != '/bin/false' or
            (allocation.get('uid') is not None and account.pw_uid != allocation['uid'])):
        raise ValueError('Refusing foreign VMM account: ' + allocation['account'])
    return account


def prepare_account(config, allocation):
    account = check_account(config, allocation)
    if account is None:
        group = check_group(allocation)
        if group is None:
            allocation['groupPending'] = 'create'
            save(Path(config['state']) / 'runtime' / allocation['id'] / 'managed.json', allocation)
            host.run('groupadd', '--system', allocation['account'])
            group = check_group(allocation)
        allocation['gid'] = group.gr_gid
        allocation['groupPending'] = None
        save(Path(config['state']) / 'runtime' / allocation['id'] / 'managed.json', allocation)
        host.run('useradd', '--system', '--no-user-group', '--gid', str(group.gr_gid), '--no-create-home',
                 '--home-dir', '/var/empty', '--shell', '/bin/false', '--comment',
                 'koi-' + config['owner'] + '-' + allocation['id'], allocation['account'])
        allocation['uid'] = None
        account = check_account(config, allocation)
    allocation['uid'] = account.pw_uid
    allocation['gid'] = account.pw_gid
    grant_kvm(allocation['account'])
    save(Path(config['state']) / 'runtime' / allocation['id'] / 'managed.json', allocation)
    return account


def grant_kvm(account):
    device = Path('/dev/kvm').stat()
    if stat.S_IMODE(device.st_mode) & 0o006 != 0o006:
        if device.st_gid == 0 or stat.S_IMODE(device.st_mode) & 0o060 != 0o060:
            raise ValueError('KVM needs other rw access or an existing nonroot rw device group')
        host.run('usermod', '--append', '--groups', grp.getgrgid(device.st_gid).gr_name, account)

def check_link(config, allocation):
    values = [link for link in host.links() if link['ifname'] == allocation['tap']]
    if not values:
        return None
    link = values[0]
    taps = json.loads(host.run('ip', '-j', 'tuntap', 'show'))
    tap = next((value for value in taps if value['ifname'] == allocation['tap']), {})
    if (tap.get('user') != allocation.get('uid') or 'tap' not in tap.get('flags', []) or
            link.get('ifalias', '') not in ('', 'koi:' + config['owner'] + ':' + allocation['id']) or
            link.get('master') not in (None, config['bridge'])):
        raise ValueError('Refusing foreign TAP: ' + allocation['tap'])
    return link


def prepare_link(config, allocation):
    check_account(config, allocation)
    if check_link(config, allocation) is None:
        host.run('ip', 'tuntap', 'add', 'dev', allocation['tap'], 'mode', 'tap',
                 'user', str(allocation['uid']))
    host.run('ip', 'link', 'set', allocation['tap'], 'alias',
             'koi:' + config['owner'] + ':' + allocation['id'], 'master', config['bridge'], 'up')
    if allocation.get('network') == 'static-v1':
        host.run('ip', 'neigh', 'replace', allocation['address'], 'lladdr', allocation['mac'],
                 'nud', 'reachable', 'dev', config['bridge'])


def check_allocation(config, allocation):
    identifier(allocation['id'], r'[0-9a-f]{32}')
    if allocation['account'] != 'tkv' + allocation['id'][:12] or allocation['tap'] != 'tkt' + allocation['id'][:12]:
        raise ValueError('Changed VM resource binding')
    check_account(config, allocation)
    check_group(allocation)
    check_link(config, allocation)


def requires_dhcp(config):
    legacy = any(value.get('network') != 'static-v1'
                 for value in allocations(config) if not value.get('deleting'))
    if config.get('image') is None:
        return legacy
    image = host.private(Path(config['state']) / 'images' / (config['image'] + '.json'))
    return image.get('staticNetworking') != 1 or legacy


def write_hosts(config):
    values = sorted((value for value in allocations(config) if not value.get('deleting')),
                    key=lambda value: value['name'])
    text = ''.join(value['mac'] + ',' + value['address'] + ',' + value['name'] + ',infinite\n'
                   for value in values if value.get('network') != 'static-v1')
    host.write_file(config, Path(config['hosts']), text)
    names = ''.join(value['address'] + ' ' + value['name'] + '.koi ' + value['name'] + '\n'
                    for value in values)
    host.write_file(config, Path(config['hosts']).with_name('dns-hosts'), names)


def update_hosts(config):
    write_hosts(config)
    host.refresh_dns(config)


def provision(config, name, trace=None, *, launch=None):
    from runtime import Runtime
    identifier(name, r'[a-z][a-z0-9-]{0,30}')
    if config['status'] != 'installed':
        raise ValueError('Finish koi install before creating machines')
    runtime = Runtime(config['state'], trace=trace)
    existing = runtime.store.state / 'machines' / (name + '.json')
    generation = json.loads(existing.read_text())['image'] if existing.exists() else config['image']
    if generation is None:
        raise ValueError('No default image; import a finished image with sudo koi image import DIRECTORY')
    from concurrent.futures import ThreadPoolExecutor
    with runtime.store.locked(), ThreadPoolExecutor(max_workers=1) as worker:
        record, image, fresh = runtime.store._reserve(name, generation, trace=trace)
        host.private(runtime.store.state / 'images' / (record['image'] + '.json'))
        static_network = image.get('staticNetworking') == 1
        directory = runtime.directory(record)
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / 'managed.json'
        def clone():
            runtime.store._clone(record, image, fresh, trace=trace)
            if trace is not None:
                trace.mark('provision.disk.end')
        disk_work = worker.submit(clone)
        if path.exists():
            allocation = host.private(path)
            if allocation.get('deleting'):
                raise ValueError('Deletion is pending; finish koi rm first')
        else:
            occupied = {value['address'] for value in allocations(config)}
            occupied.update(value['address'] for value in leases(config))
            network = ipaddress.IPv4Network(config['subnet'])
            address = next((str(network.network_address + slot) for slot in range(2, 255)
                            if str(network.network_address + slot) not in occupied), None)
            if address is None:
                raise ValueError('Guest subnet is full')
            allocation = {'id': record['id'], 'name': name, 'uid': None,
                          'account': 'tkv' + record['id'][:12], 'tap': 'tkt' + record['id'][:12],
                          'address': address, 'mac': '02:' + ':'.join(record['id'][i:i+2] for i in range(0, 10, 2)),
                          'cid': int(ipaddress.IPv4Address(address)) % 256 + 3,
                          'network': 'static-v1' if static_network else 'dhcp'}
            save(path, allocation)                                               
        if trace is not None:
            trace.mark('provision.allocation.end')
        prepare_account(config, allocation)
        if trace is not None:
            trace.mark('provision.account.end')
        prepare_link(config, allocation)
        if trace is not None:
            trace.mark('provision.tap.end')
        update_hosts(config)
        if trace is not None:
            trace.mark('provision.dns.end')
        configuration = directory / 'config.json'
        if not configuration.exists():
            for folder, mode in [('boots', 0o700), ('jail', 0o755)]:
                (directory / folder).mkdir(mode=mode, exist_ok=True)
                (directory / folder).chmod(mode)
            certificate_auth = image.get('sshCertificates') == 1
            network = {'address': allocation['address'] + '/24', 'gateway': config['gateway'],
                       'dns': config['gateway']} if static_network else None
            if certificate_auth:
                import access
                public_key = access.paths(runtime.store.state)['ca'].with_suffix('.pub')
                access.check(runtime.store.state)
            else:
                key = directory / 'management-key'
                if not key.exists():
                    temporary = directory / 'management-key.pending'
                    if temporary.exists():
                        raise ValueError('Incomplete management key retained; inspect before retrying')
                    host.run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', '', '-f', str(temporary))
                    os.replace(temporary, key)
                    os.replace(Path(str(temporary) + '.pub'), directory / 'management-key.pub')
                public_key = directory / 'management-key.pub'
            if trace is not None:
                trace.mark('provision.authentication.end')
            seed = directory / 'seed.img'
            if not seed.exists():
                temporary = directory / 'seed.pending'
                if temporary.exists():
                    expected = {'name': name, 'id': record['id'], 'ssh_key': public_key.read_text()}
                    if certificate_auth:
                        expected['auth'] = 'certificate-v1'
                    if network is not None:
                        expected['network'] = network
                    trial.check_seed(temporary, expected)
                else:
                    trial.prepare_seed(SimpleNamespace(name=name, identity=record['id'],
                                                       public_key=public_key, output=temporary,
                                                       certificate_auth=certificate_auth, network=network))
                os.replace(temporary, seed)
            if trace is not None:
                trace.mark('provision.seed.end')
            seed.chmod(0o444)
            if not certificate_auth:
                key.chmod(0o600)
            (runtime.store.state / 'kernels' / record['image']).chmod(0o444)
            disk_work.result()                                                              
            save(configuration, {'id': record['id'], 'name': name, 'image': record['image'],
                                  'address': allocation['address'], 'hostKey': None,
                                  'account': allocation['account'], 'tap': allocation['tap'],
                                  'mac': allocation['mac'], 'cid': allocation['cid'],
                                  'networkUnit': config['networkUnit'], 'dnsUnit': config['dnsUnit'],
                                  'vmm': config['vmm'], 'closure': config['closure'],
                                  'boot': None, 'pending': None,
                                  'inferenceRelay': image.get('inferenceRelay'),
                                  'inferenceGranted': image.get('inferenceRelay') == 1,
                                  'auth': 'certificate-v1' if certificate_auth else 'public-key'})
        disk_work.result()                                                                   
        if trace is not None:
            trace.mark('provision.configuration.end')
        if launch is not None:
            launch(runtime, record)
    return runtime


def remove_machine(config, name, delete_disk=False):
    from runtime import Runtime, UNITS, stopped
    runtime = Runtime(config['state'])
    path = runtime.store.state / 'machines' / (name + '.json')
    record = host.private(path)
    directory = runtime.directory(record)
    allocation_path = directory / 'managed.json'
    allocation = host.private(allocation_path) if allocation_path.exists() else None
    if allocation:
        check_allocation(config, allocation)
    import inference
    inference.check_units(config, record)
    volume = runtime.store.dataset + '/machines/' + record['id']
    if (directory / 'config.json').exists():
        if runtime.store.exists(volume):
            runtime.stop(name)
        elif not stopped(runtime.unit(record)):
            raise ValueError('Disk absent while VMM is live; retained for diagnosis')
        unit = UNITS / runtime.unit(record)
        if unit.exists():
            runtime.check_unit(record, runtime.configuration(record))
            unit.unlink()
    inference.remove(config, record)
    if allocation:
        allocation['deleting'] = delete_disk
        save(allocation_path, allocation)
        if delete_disk:
            release_lease(config, allocation)
        if check_link(config, allocation) is not None:
            host.run('ip', 'link', 'delete', allocation['tap'])
        account = check_account(config, allocation)
    else:
        account = None
    volume = runtime.store.dataset + '/machines/' + record['id']
    if runtime.store.exists(volume):
        runtime.store.owned(volume, 'volume', runtime.store.dataset + '/images/' + record['image'] + '@base')
        disk = Path('/dev/zvol') / volume
        if disk.exists():
            os.chown(disk, 0, 0)
            disk.chmod(0o600)
        if delete_disk:
            zfs('destroy', volume)
    if account is not None:
        host.run('userdel', allocation['account'])
    if allocation is not None and check_group(allocation) is not None:
        host.run('groupdel', allocation['account'])
    if delete_disk:
        if directory.exists():
            shutil.rmtree(directory)
        path.unlink()


def delete_machine(config, name):
    """Complete ordinary owned deletion; caller holds the host operation lock."""
    remove_machine(config, name, delete_disk=True)
    update_hosts(config)
    host.run('systemctl', 'daemon-reload')


def inventory(config):
    store = Disks(config['state'])
    names = zfs('list', '-H', '-o', 'name', '-t', 'filesystem,volume,snapshot').splitlines()
    return store, {name for name in names if name == store.dataset or name.startswith(store.dataset + '/')}


def check_storage_removal(config):
    store, actual = inventory(config)
    expected = {store.dataset, store.dataset + '/images', store.dataset + '/machines'}
    for path in (store.state / 'machines').glob('*.json'):
        record = host.private(path)
        identifier(record['id'], r'[0-9a-f]{32}')
        volume = store.dataset + '/machines/' + record['id']
        expected.add(volume)
        if volume in actual:
            store.owned(volume, 'volume', store.dataset + '/images/' + record['image'] + '@base')
    for path in (store.state / 'images').glob('*.json'):
        record = host.private(path)
        identifier(record['id'], r'[0-9a-f]{64}')
        volume = store.dataset + '/images/' + record['id']
        expected.update((volume, volume + '@base'))
        for resource, kind in ((volume, 'volume'), (volume + '@base', 'snapshot')):
            if resource in actual:
                store.owned(resource, kind)
        if volume + '@base' in actual:
            clones = zfs('get', '-H', '-o', 'value', 'clones', volume + '@base')
            if clones != '-' and any(clone not in expected and not clone.startswith(store.dataset + '/machines/')
                                     for clone in clones.split(',')):
                raise ValueError('Base image has foreign dependent clones')
    if actual - expected:
        raise ValueError('Unexpected storage children retained: ' + ', '.join(sorted(actual - expected)))
    for resource in (store.dataset, store.dataset + '/images', store.dataset + '/machines'):
        if resource in actual:
            store.owned(resource, 'filesystem')


def purge_storage(config):
    check_storage_removal(config)
    store, actual = inventory(config)
    for path in (store.state / 'images').glob('*.json'):
        record = host.private(path)
        volume = store.dataset + '/images/' + record['id']
        for resource in (volume + '@base', volume):
            if resource in actual:
                zfs('destroy', resource)
    for resource in (store.dataset + '/machines', store.dataset + '/images', store.dataset):
        if resource in actual:
            zfs('destroy', resource)
