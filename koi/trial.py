#!/usr/bin/env python3
"""Prepare public seed data or launch one direct-boot trial on a selected host."""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import uuid


import seed as guest


def prepare_seed(args):
    seed = guest.validate({"name": args.name, "id": getattr(args, "identity", None) or uuid.uuid4().hex,
                           "ssh_key": args.public_key.read_text()})
    if getattr(args, 'network', None) is not None:
        seed['network'] = guest.network(args.network)
    if getattr(args, 'certificate_auth', False):
        seed['auth'] = 'certificate-v1'
    with args.output.open("xb") as stream:
        stream.truncate(16 * 1024**2)
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "config.json").write_text(json.dumps(seed) + "\n")
        for field, filename in [('name', 'name'), ('id', 'id'), ('ssh_key', 'authorized_keys')]:
            Path(directory, filename).write_text(seed[field] + '\n')
        if getattr(args, 'certificate_auth', False):
            Path(directory, 'certificate-auth').write_text('1\n')
        if 'network' in seed:
            Path(directory, 'network').write_text(''.join(seed['network'][field] + '\n'
                                                       for field in ('address', 'gateway', 'dns')))
        subprocess.run(["mkfs.ext4", "-q", "-L", "koi-seed", "-d", directory,
                        str(args.output)], check=True)
    return seed


def check_seed(image, expected):
    """Validate an interrupted seed before adopting its public guest binding."""
    def read_file(filename):
        result = subprocess.run(["debugfs", "-R", "cat /" + filename, str(image)],
                                capture_output=True, text=True, check=True, timeout=35)
        return result.stdout

    raw = json.loads(read_file('config.json'))
    value = guest.validate(raw)
    binding = guest.validate(expected)
    if value != binding or raw.get('auth') != expected.get('auth'):
        raise ValueError('Interrupted seed differs from machine')
    fields = {'name': binding['name'], 'id': binding['id'], 'authorized_keys': binding['ssh_key']}
    if expected.get('auth') == 'certificate-v1':
        fields['certificate-auth'] = '1'
    else:
        marker = subprocess.run(['debugfs', '-R', 'stat /certificate-auth', str(image)],
                                capture_output=True, text=True, check=True, timeout=35)
        if marker.stdout:
            raise ValueError('Unexpected certificate seed marker')
    if 'network' in binding:
        fields['network'] = '\n'.join(binding['network'][field] for field in ('address', 'gateway', 'dns'))
    for filename, contents in fields.items():
        if read_file(filename) != contents + '\n':
            raise ValueError('Interrupted seed field is incomplete or differs: /' + filename)


def command(args):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,15}", args.tap):
        raise ValueError("Invalid TAP interface name")
    paths = [args.disk.resolve(), args.kernel.resolve(), args.seed.resolve(), args.runtime.resolve()]
    if any("," in str(p) or "\n" in str(p) for p in paths):
        raise ValueError("VMM paths cannot contain commas or newlines")
    mac = getattr(args, 'mac', '02:00:00:00:00:03')
    cid = getattr(args, 'cid', 3)
    if not re.fullmatch(r'02(:[0-9a-f]{2}){5}', mac) or not 3 <= cid <= 0xffffffff:
        raise ValueError('Invalid managed network identity')
    return ["cloud-hypervisor", "--kernel", str(paths[1]),
            "--cmdline", "root=/dev/vda1 rootwait rw console=ttyS0 net.ifnames=0 swiotlb=noforce",
            "--disk", "path=" + str(paths[0]) + ",image_type=raw,sparse=off,direct=on",
            "path=" + str(paths[2]) + ",image_type=raw,readonly=on",
            "--cpus", "boot=2", "--memory", "size=4G,prefault=off",
            "--balloon", "size=0,free_page_reporting=on",
            "--net", "tap=" + args.tap + ",mac=" + mac,
            "--vsock", "cid=" + str(cid) + ",socket=" + str(paths[3] / "vsock"),
            "--api-socket", str(paths[3] / "api"),
            "--serial", "tty", "--console", "off"]


def launch(args):
    if os.geteuid() == 0:
        raise ValueError("Run the trial VMM as an unprivileged test account")
    for path in (args.disk, args.kernel, args.seed):
        if not path.is_file() and not (path == args.disk and path.exists()
                                       and stat.S_ISBLK(path.stat().st_mode)):
            raise ValueError("Missing prepared trial artifact: " + str(path))
    argv = command(args)
    args.runtime.mkdir(mode=0o700)                                                     
    subprocess.run(argv, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True)
    seed = commands.add_parser("seed")
    seed.add_argument("name")
    seed.add_argument("public_key", type=Path)
    seed.add_argument("output", type=Path)
    seed.add_argument("--id", dest="identity", help="Identity from the prepared ZFS machine record")
    seed.add_argument('--address', help='Guest IPv4 address with /24 prefix, for current images')
    seed.add_argument('--gateway', help='Host bridge address; also used for guest DNS')
    seed.set_defaults(run=prepare_seed)
    boot = commands.add_parser("boot")
    boot.add_argument("kernel", type=Path)
    boot.add_argument("disk", type=Path)
    boot.add_argument("seed", type=Path)
    boot.add_argument("tap", help="Prepared TAP owned by the unprivileged test account")
    boot.add_argument("runtime", type=Path, help="New directory for this VMM's sockets")
    boot.set_defaults(run=launch)
    args = parser.parse_args()
    if args.run == prepare_seed and (args.address is not None or args.gateway is not None):
        if args.address is None or args.gateway is None:
            parser.error('Supply both --address and --gateway for fixed guest networking')
        args.network = {'address': args.address, 'gateway': args.gateway, 'dns': args.gateway}
    args.run(args)
