"""Opt-in, bounded boot diagnostics; never an authentication input.

Host milestones share CLOCK_MONOTONIC. Guest CLOCK_BOOTTIME is bracketed by a
single readiness-channel exchange. Cloud Hypervisor's relative event origin is
bounded by a pre-exec timestamp and receipt of its first event.
Neither transport delay nor unrelated clock origins are assumed to be zero.
"""

import json
import os
from pathlib import Path
import re
import stat
import threading
import time


def objects(text):
    """Cloud Hypervisor emits successive pretty-printed JSON objects."""
    decoder = json.JSONDecoder()
    while text.strip():
        value, end = decoder.raw_decode(text.lstrip())
        yield value
        text = text.lstrip()[end:]


def read_bounded(path, limit=1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Trace input must be a regular file')
        text = stream.read(limit + 1)
    if len(text) > limit:
        raise ValueError('Oversized trace input')
    return text


class StartupTrace:
    def __init__(self, began=None):
        self.events = []
        self.mark('controller.enter', began)
        self.calibration = None
        self.first_vmm = None
        self.stop = threading.Event()
        self.reader = None
        self.binding = None

    def mark(self, name, timestamp=None):
        self.events.append({'name': name, 'host_ns': time.monotonic_ns() if timestamp is None else timestamp})

    def watch_vmm(self, path):
        def read_first():
            deadline = time.monotonic() + 30
            while not self.stop.is_set() and time.monotonic() < deadline:
                try:
                    text = read_bounded(path, 65536)
                    value = next(objects(text))
                    if (value['source'], value['event']) == ('vmm', 'starting'):
                        self.first_vmm = {'event': value, 'received_host_ns': time.monotonic_ns()}
                    return
                except (OSError, ValueError, KeyError, StopIteration):
                    self.stop.wait(0.001)
        self.reader = threading.Thread(target=read_first, daemon=True)
        self.reader.start()

    def bind(self, path, record, boot):
        self.binding = (path, record, boot)

    def finish(self):
        self.stop.set()
        if self.reader is not None:
            self.reader.join(timeout=1)
        if self.binding is None:
            return
        path, record, boot = self.binding
        document = {'version': 1, 'machine': record['id'], 'boot': boot,
                    'events': self.events, 'guest_exchange': self.calibration,
                    'first_vmm': self.first_vmm}
        path.write_text(json.dumps(document, indent=2) + '\n')
        self.binding = None


def offset(exchange):
    values = [exchange[key] for key in ('host_send_ns', 'guest_receive_ns', 'guest_send_ns', 'host_receive_ns')]
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError('Invalid guest trace clock exchange')
    h1, g2, g3, h4 = values
    low, high = h1 - g2, h4 - g3
    if h4 < h1 or g3 < g2 or low > high:
        raise ValueError('Inconsistent guest trace clock exchange')
    return low, high


def event_ns(value):
    timestamp = value['timestamp']
    seconds, nanos = timestamp['secs'], timestamp['nanos']
    if type(seconds) is not int or type(nanos) is not int or seconds < 0 or not 0 <= nanos < 10**9:
        raise ValueError('Invalid VMM event timestamp')
    return seconds * 10**9 + nanos


def breakdown(document, vmm_text, guest_text, kernel_text, exec_ns, prompt_ns, began_ns):
    """Keep raw clocks and uncertainty intervals alongside the readable phases."""
    host = {value['name']: value['host_ns'] for value in document['events']}
    guest_low, guest_high = offset(document['guest_exchange'])
    first = document['first_vmm']
    if first is None:
        raise ValueError('Missing VMM starting event; inspect raw trace')
    vmm_low = exec_ns
    vmm_high = first['received_host_ns'] - event_ns(first['event'])
    if not 0 < vmm_low <= vmm_high:
        raise ValueError('Inconsistent VMM clock bounds; inspect raw trace')
    vmm = {}
    for value in objects(vmm_text):
        name = value['source'] + '.' + value['event']
        device = (value.get('properties') or {}).get('id')
        if device is not None:
            name += ':' + str(device)
        vmm[name] = event_ns(value)
    guest = {}
    for line in guest_text.splitlines():
        timestamp, name = line.split('\t', 1)
        ns = round(float(timestamp) * 10**9)
        if not 0 <= ns <= 120 * 10**9 or not re.fullmatch(r'[a-z0-9_.]+', name):
            raise ValueError('Invalid guest trace milestone')
        guest[name] = ns
    guest['ready.send'] = document['guest_exchange']['guest_send_ns']
    match = re.search(r'\[\s*([0-9.]+)\]\s+Run /sbin/init', kernel_text)
    if not match:
        raise ValueError('Missing kernel init milestone; inspect raw kernel log')
    init = round(float(match[1]) * 10**9)
    milestones = [(name, ns, ns) for name, ns in host.items()]
    milestones.extend(('vmm:' + name, ns + vmm_low, ns + vmm_high) for name, ns in vmm.items())
    milestones.extend(('guest:' + name, ns + guest_low, ns + guest_high) for name, ns in guest.items())
    milestones.append(('guest:kernel.init', init + guest_low, init + guest_high))
    milestones.append(('host.vmm-exec', exec_ns, exec_ns))
    milestones.extend([('cli.invocation', began_ns, began_ns), ('prompt', prompt_ns, prompt_ns)])
    indexed = {name: (low, high) for name, low, high in milestones}

    def interval(label, left, right):
        a, b = indexed[left], indexed[right]
        return {'name': label, 'min_ms': (b[0] - a[1]) / 10**6, 'max_ms': (b[1] - a[0]) / 10**6}

    prefix = [('CLI entry / dispatch', 'cli.invocation', 'validation.begin')]
    if 'provision.begin' in host:
        prefix = [('CLI wrapper / interpreter / helper', 'cli.invocation', 'controller.enter'),
                  ('Controller configuration / dispatch', 'controller.enter', 'provision.begin'),
                  ('Fresh machine provisioning', 'provision.begin', 'provision.end'),
                  ('Start dispatch', 'provision.end', 'validation.begin')]
    if 'host.operation-lock.begin' in host and 'host.operation-lock.end' in host:
        dispatch_end = 'provision.begin' if 'provision.begin' in host else 'validation.begin'
        entry = [
            ('CLI wrapper / interpreter / helper', 'cli.invocation', 'controller.enter'),
            ('Controller configuration', 'controller.enter', 'host.operation-lock.begin'),
            ('Wait for serialized host operation', 'host.operation-lock.begin', 'host.operation-lock.end'),
            ('Controller dispatch', 'host.operation-lock.end', dispatch_end)]
        prefix = entry + prefix[2:] if 'provision.begin' in host else entry
    validation = [('Host validation', 'validation.begin', 'validation.end')]
    if 'ssh.certificate.begin' in host:
        validation = [('Host validation', 'validation.begin', 'ssh.certificate.begin'),
                      ('VM-scoped SSH certificate signing', 'ssh.certificate.begin', 'ssh.certificate.end'),
                      ('Validation completion', 'ssh.certificate.end', 'validation.end')]
    attachment = [('Bookkeeping / interactive attach / prompt', 'ssh.end', 'prompt')]
    if 'attach.exec' in host:
        attachment = [('Save lifecycle completion / prepare attach', 'ssh.end', 'attach.exec'),
                      ('Execute attach / SSH session / shell prompt', 'attach.exec', 'prompt')]
    phases = [interval(label, left, right) for label, left, right in prefix + validation + [
        ('Host boot preparation', 'validation.end', 'launch.request'),
        ('Host systemd launch / confinement', 'launch.request', 'host.vmm-exec'),
        ('VMM exec / loader / monitor setup', 'host.vmm-exec', 'vmm:vmm.starting'),
        ('VMM initialization to vCPUs started event', 'vmm:vmm.starting', 'vmm:vm.booted'),
        ('vCPUs started event to kernel init (mapped printk clock)', 'vmm:vm.booted', 'guest:kernel.init'),
        ('Guest userspace to ready-send', 'guest:kernel.init', 'guest:ready.send'),
        ('Readiness transport / identity validation', 'guest:ready.send', 'ready.validated'),
        ('Host enrollment / SSH preparation', 'ready.validated', 'ssh.begin'),
        ('SSH authentication / verification', 'ssh.begin', 'ssh.end'),
    ] + attachment]
    for phase in phases:
        if phase['name'] == 'VMM initialization to vCPUs started event':
            phase['min_ms'] = phase['max_ms'] = (vmm['vm.booted'] - vmm['vmm.starting']) / 10**6
        if phase['name'] == 'Guest userspace to ready-send':
            phase['min_ms'] = phase['max_ms'] = (guest['ready.send'] - init) / 10**6
    previous = init
    guest_steps = []
    for name, ns in sorted(guest.items(), key=lambda item: item[1]):
        if init <= ns <= guest['ready.send'] and name != 'services.session':
            guest_steps.append({'through': name, 'elapsed_ms': (ns - previous) / 10**6,
                                'guest_boot_ms': ns / 10**6})
            previous = ns
    provision_steps = []
    previous = 'provision.begin'
    provision_marks = ('provision.disk.end', 'provision.allocation.end', 'provision.account.end',
                       'provision.tap.end', 'provision.dns.end', 'provision.authentication.end',
                       'provision.seed.end', 'provision.configuration.end')
    for name in sorted((name for name in provision_marks if name in indexed),
                       key=lambda name: indexed[name][0]):
        if name in indexed:
            provision_steps.append({'through': name, 'elapsed_ms': (indexed[name][0] - indexed[previous][0]) / 10**6})
            previous = name
    disk_steps = []
    previous = 'provision.begin'
    for name in ('provision.disk.properties.end', 'provision.disk.image-validation.end',
                 'provision.disk.intent.end', 'provision.disk.existence.end',
                 'provision.disk.clone.end', 'provision.disk.clone-validation.end', 'provision.disk.end'):
        if name in indexed:
            disk_steps.append({'through': name, 'elapsed_ms': (indexed[name][0] - indexed[previous][0]) / 10**6})
            previous = name
    return {'phases': phases, 'host_disk_steps': disk_steps, 'host_provision_steps': provision_steps, 'kernel_to_init_ms': init / 10**6,
            'guest_userspace_steps': guest_steps,
            'clock_bounds': {'guest_offset_ns': [guest_low, guest_high],
                             'vmm_origin_host_ns': [vmm_low, vmm_high]},
            'milestones': [{'name': name, 'host_low_ns': low, 'host_high_ns': high,
                            'entry_low_ms': (low - began_ns) / 10**6,
                            'entry_high_ms': (high - began_ns) / 10**6}
                           for name, low, high in sorted(milestones, key=lambda value: value[1])],
            'notes': ['Opt-in tracing changes the workload; compare ordinary bench separately.',
                      'VMM booted is emitted after starting vCPUs; it is not first guest instruction.',
                      'Kernel printk time is mapped to guest boottime assuming the unsuspended guest clocks agree.',
                      'Guest markers use CLOCK_BOOTTIME; reading/writing a marker adds diagnostic work.',
                      'Cross-clock bounds share correlated offsets: do not sum all minima or maxima.',
                      'Events describe elapsed critical-path milestones, not CPU execution time.']}


def collect(state, name, output, sample):
    """Post-prompt inspection finishes before scheduling fixture cleanup."""
    import channels
    import runtime
    controller = runtime.Runtime(state)
    with controller.store.locked():
        record = controller.machine(name)
        config = controller.configuration(record)
        controller.check_unit(record, config, live=True)
        directory = controller.directory(record)
        document = json.loads(read_bounded(directory / 'start-trace.json'))
        if (document['machine'], document['boot']) != (record['id'], config['boot']):
            raise ValueError('Startup trace belongs to another boot')
        work = directory / 'boots' / config['boot']
        vmm_text = read_bounded(work / 'vmm-events.json')
        exec_text = read_bounded(work / 'exec-start.ns', 64)
        exec_ns = int(exec_text)
        connection = controller.args(record, config)
    (output.with_suffix('.host.json')).write_text(json.dumps(document, indent=2) + '\n')
    (output.with_suffix('.vmm.json')).write_text(vmm_text)
    (output.with_suffix('.exec.ns')).write_text(exec_text)
    guest = channels.ssh(connection, 'sudo -n cat /run/koi-boot.tsv').stdout
    kernel = channels.ssh(connection, 'sudo -n dmesg').stdout
    output.with_suffix('.guest.tsv').write_text(guest)
    output.with_suffix('.kernel.log').write_text(kernel)
    result = breakdown(document, vmm_text, guest, kernel, exec_ns,
                       sample['prompt_host_ns'], sample['began_host_ns'])
    output.with_suffix('.json').write_text(json.dumps(result, indent=2) + '\n')
    return result
