"""Sequential CLI-to-prompt timing; successful fixtures only are removed."""

import errno
import json
import os
from pathlib import Path
import pty
import select
import signal
import stat
import statistics
import subprocess
import sys
import time
import uuid


def entry(executable, operation, name, output, timeout=40, trace=False):
    began_ns = time.monotonic_ns()
    began = began_ns / 10**9
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execv(str(executable), [str(executable), operation, *(['--trace'] if trace else []), name])
        finally:
            os._exit(127)
    terminal = bytearray()
    prompt = ('dev@' + name + ':~/projects/' + name + '$ ').encode()
    elapsed = None
    timings = {}
    reaped = False
    try:
        deadline = began + timeout
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError('Guest prompt/input timed out; fixture retained')
            if not select.select([fd], [], [], min(0.1, max(0, deadline - time.monotonic())))[0]:
                continue
            try:
                block = os.read(fd, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                block = b''
            if not block:
                break
            terminal.extend(block)
            if len(terminal) > 8 * 1024 * 1024:
                raise RuntimeError('Excessive terminal output; fixture retained')
            if elapsed is None and prompt in terminal:
                prompt_ns = time.monotonic_ns()
                elapsed = (prompt_ns - began_ns) / 10**9
                os.write(fd, b'printf "\\n__ENTRY_INPUT_OK__\\n"; exit\n')
        while True:
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                reaped = True
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('CLI did not exit; fixture retained')
            time.sleep(0.01)
        if elapsed is None or status != 0 or b'\r\n__ENTRY_INPUT_OK__\r\n' not in terminal:
            raise RuntimeError('Guest prompt/input/exit verification failed; fixture retained')
        timings.update(seconds=elapsed, verification_seconds=time.monotonic() - began - elapsed)
        timings.update(began_host_ns=began_ns, prompt_host_ns=prompt_ns)
        return timings
    finally:
        os.close(fd)
        if not reaped:
            deadline = time.monotonic() + 1
            while not os.waitpid(pid, os.WNOHANG)[0]:
                if time.monotonic() >= deadline:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                    break
                time.sleep(0.01)
        output.write_bytes(terminal)
        timings['entry_wall_seconds'] = time.monotonic() - began


def remove_successful(executable, name, state):
    """Normal cleanup, with only the asynchronous shutdown wait unlocked."""
    import host
    import managed
    from runtime import Runtime
    with host.operation_lock():
        config = host.load(state)
        controller = Runtime(config['state'])
        ticket = controller.begin_stop(name)
        if ticket is None:
            managed.delete_machine(config, name)
            return
    controller.wait_stop(ticket)
    with host.operation_lock():
        controller.finish_stop(ticket)
        latest = host.load(state)
        if any(latest[key] != config[key] for key in ('state', 'owner', 'dataset')):
            raise ValueError('Installation changed during shutdown; refusing cleanup')
        managed.delete_machine(latest, name)


def cleanup_job(executable, name, state, output, began):
    pid = os.fork()
    if pid:
        return pid
    result = {'name': name, 'result': 'failed', 'started_seconds': time.monotonic() - began}
    try:
        os.setsid()
        with output.with_suffix('.log').open('wb') as log:
            os.dup2(log.fileno(), 1)
            os.dup2(log.fileno(), 2)
            remove_successful(executable, name, state)
        result['result'] = 'passed'
    except BaseException as error:
        result['error'] = str(error) or type(error).__name__
    finally:
        result['finished_seconds'] = time.monotonic() - began
        result['seconds'] = result['finished_seconds'] - result['started_seconds']
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            output.write_text(json.dumps(result, indent=2) + '\n')
        finally:
            os._exit(0 if result['result'] == 'passed' else 1)


def run(executable, mode, samples, output, state=None, trace=False):
    began = time.monotonic()
    if mode not in ('fresh', 'retained') or not 1 <= samples <= 10:
        raise ValueError('Choose fresh or retained mode and 1–10 samples')
    output = Path(output)
    output.mkdir(mode=0o700)
    prefix = 'bench-' + uuid.uuid4().hex[:10]
    report = {'mode': mode, 'executable': str(executable), 'samples': [], 'result': 'failed',
              'fixture': prefix, 'cleanup_errors': [], 'trace': trace}
    jobs = []

    def command(*args):
        command_began = time.monotonic()
        with (output / 'lifecycle.log').open('ab') as log:
            log.write(('koi ' + ' '.join(args) + '\n').encode())
            log.flush()
            subprocess.run([str(executable), *args], check=True, timeout=120,
                           stdout=log, stderr=subprocess.STDOUT)
        return time.monotonic() - command_began

    def enqueue(sample, index):
        path = output / (str(index) + '-cleanup.json')
        jobs.append((cleanup_job(executable, sample['name'], state, path, began), path, sample))

    print(f'{mode}: CLI invocation → usable guest prompt ({samples} samples)', flush=True)
    print(f'Logs: {output}', flush=True)
    if trace:
        print('Tracing enabled: diagnostic workload, with explicit clock bounds.', flush=True)
    try:
        if mode == 'retained':
            command('new', prefix, '--no-attach', *(['--trace'] if trace else []))
        report['setup_seconds'] = time.monotonic() - began
        for index in range(samples):
            sample_began = time.monotonic()
            name = prefix + '-' + str(index) if mode == 'fresh' else prefix
            report['fixture'] = name
            sample = {'name': name, 'seconds': None, 'stop_before_seconds': 0,
                      'started_seconds': sample_began - began}
            report['samples'].append(sample)
            if mode == 'retained':
                sample['stop_before_seconds'] = command('stop', name)
            sample.update(entry(executable, 'new' if mode == 'fresh' else 'attach',
                                name, output / (str(index) + '-terminal.log'), trace=trace))
            sample['trace_collection_seconds'] = 0
            if trace:
                import startup_trace
                collection_began = time.monotonic()
                sample['trace'] = startup_trace.collect(state, name, output / (str(index) + '-trace'), sample)
                sample['trace_collection_seconds'] = time.monotonic() - collection_began
                for phase in sample['trace']['phases']:
                    low, high = phase['min_ms'], phase['max_ms']
                    value = f'{low:.1f}' if low == high else f'[{low:.1f}, {high:.1f}]'
                    print(f"    {phase['name']}: {value} ms", flush=True)
                if sample['trace'].get('host_provision_steps'):
                    print('    Host provisioning detail (elapsed since preceding marker):', flush=True)
                    for step in sample['trace']['host_provision_steps']:
                        print(f"      → {step['through']}: {step['elapsed_ms']:.1f} ms", flush=True)
                if sample['trace'].get('host_disk_steps'):
                    print('    Disk provisioning detail (elapsed since preceding marker):', flush=True)
                    for step in sample['trace']['host_disk_steps']:
                        print(f"      → {step['through']}: {step['elapsed_ms']:.1f} ms", flush=True)
                print('    Guest userspace detail (elapsed since preceding marker):', flush=True)
                for step in sample['trace']['guest_userspace_steps']:
                    print(f"      → {step['through']}: {step['elapsed_ms']:.1f} ms", flush=True)
                bounds = sample['trace']['clock_bounds']
                print('    Clock uncertainty: guest ' +
                      f"{(bounds['guest_offset_ns'][1] - bounds['guest_offset_ns'][0]) / 10**6:.2f} ms; VMM " +
                      f"{(bounds['vmm_origin_host_ns'][1] - bounds['vmm_origin_host_ns'][0]) / 10**6:.2f} ms", flush=True)
            sample['total_seconds'] = time.monotonic() - sample_began
            sample['overhead_seconds'] = (sample['total_seconds'] - sample['seconds'] -
                                         sample['verification_seconds'] - sample['stop_before_seconds'] -
                                         sample['trace_collection_seconds'])
            print(f"  {index + 1}/{samples}: prompt {sample['seconds']:.3f} s; "
                  f"verify/SSH exit {sample['verification_seconds']:.3f} s; "
                  f"stop before {sample['stop_before_seconds']:.3f} s", flush=True)
            if mode == 'fresh':
                enqueue(sample, index)
        if mode == 'retained':
            enqueue(report['samples'][-1], samples - 1)
        values = [sample['seconds'] for sample in report['samples']]
        report['summary'] = {'min': min(values), 'median': statistics.median(values), 'max': max(values)}
        report['timing_seconds'] = time.monotonic() - began
        report['result'] = 'timed'
        summary = report['summary']
        print(f"Min {summary['min']:.3f} s · median {summary['median']:.3f} s · max {summary['max']:.3f} s", flush=True)
        print(f"Timing complete in {report['timing_seconds']:.3f} s; waiting for background cleanup…", flush=True)
    except BaseException as error:
        report['error'] = str(error) or type(error).__name__
        print(f"Failed; inspect fixture {report['fixture']} and logs at {output}", flush=True)
        raise
    finally:
        drain_began = time.monotonic()
        for pid, path, sample in jobs:
            _, status = os.waitpid(pid, 0)
            result = json.loads(path.read_text()) if path.exists() else {
                'name': sample['name'], 'result': 'failed', 'error': 'Cleanup worker exited without a report'}
            sample['cleanup'] = result
            if status != 0 or result['result'] != 'passed':
                report['cleanup_errors'].append(result)
        if report['result'] == 'timed':
            report['result'] = 'failed' if report['cleanup_errors'] else 'passed'
            report['fixture'] = report['cleanup_errors'][0]['name'] if report['cleanup_errors'] else None
        report['cleanup_wait_seconds'] = time.monotonic() - drain_began
        report['total_seconds'] = time.monotonic() - began
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f"Remaining cleanup wait {report['cleanup_wait_seconds']:.3f} s; "
          f"benchmark wall time {report['total_seconds']:.3f} s", flush=True)
    if report['cleanup_errors']:
        raise RuntimeError('Background cleanup failed; inspect fixture state and retained logs: ' +
                           ', '.join(value['name'] for value in report['cleanup_errors']))
    return report


def installed(args):
    import host
    with host.operation_lock():
        config = host.load(args.state)
        if args.trace:
            image = json.loads((Path(config['state']) / 'images' / (config['image'] + '.json')).read_text())
            if image.get('bootTrace') != 1:
                raise ValueError('bench --trace requires the updated bootTrace=1 guest image; rebuild/import it first')
        executable = Path(config['package']) / 'bin/koi'
        if not str(executable).startswith('/nix/store/') or executable.stat().st_uid != 0:
            raise ValueError('Bench requires the installed Nix-packaged command')
        directory = Path(config['state']) / 'bench'
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError('Unsafe benchmark evidence directory')
    run(executable, args.mode, args.samples, directory / uuid.uuid4().hex, config['state'], trace=args.trace)
