"""Owned ZFS image generations and persistent project clones.

Runtime isolation and liveness checks must precede deletion. Pool ownership
and VM lifecycle operations belong to their host/controller modules.
"""

import argparse
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid

OWNER = "org.koi:owner"
STATE_FORMAT = 1
UNVERSIONED_STATE_FORMAT = 1
DISK_HASH_SCHEME = "sparse-extents-sha256-v1"
CAPABILITIES = ("bootTrace", "sshCertificates", "inferenceRelay", "staticNetworking")


def check_state_format(record):
    version = record.get('stateFormat', UNVERSIONED_STATE_FORMAT)
    if type(version) is not int or version != STATE_FORMAT:
        raise ValueError(f'Unsupported Koi state format {version!r}; this build requires exactly '
                         f'{STATE_FORMAT}. State retained; use a compatible Koi build or an explicit migration.')


def progress(message):
    print("koi: " + message, file=sys.stderr, flush=True)


def byte_size(size):
    for unit, divisor in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if size >= divisor:
            return f"{size / divisor:.1f} {unit}"
    return f"{size} bytes"


def disk_extents(stream):
    """Return reported data ranges, without reading holes or their zero bytes."""
    size = os.fstat(stream.fileno()).st_size
    extents = []
    position = 0
    while position < size:
        try:
            start = os.lseek(stream.fileno(), position, os.SEEK_DATA)
        except OSError as error:
            if error.errno == errno.ENXIO:
                break                            
            if error.errno in (errno.EINVAL, errno.ENOTSUP):
                raise ValueError("Image filesystem must support SEEK_DATA/SEEK_HOLE; "
                                 "place root.img on a filesystem with sparse extent support") from error
            raise
        end = min(os.lseek(stream.fileno(), start, os.SEEK_HOLE), size)
        if not position <= start < end <= size:
            raise ValueError("Invalid image data extent")
        extents.append((start, end))
        position = end
    return size, extents


def sparse_digest(stream, *, outgoing=None, label="Hashing image data"):
    """Hash size, extent offsets/lengths and bytes; optionally copy those bytes.

    This is an extent digest, not SHA256 of the full raw disk. Allocation layout
    participates in identity. Legacy generations keep their original digest.
    """
    before = os.fstat(stream.fileno())
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Prepared disk must be a regular file")
    size, extents = disk_extents(stream)
    total = sum(end - start for start, end in extents)
    progress(f"{label}: {byte_size(total)} data / {byte_size(size)} virtual disk")
    digest = hashlib.sha256(DISK_HASH_SCHEME.encode() + b"\0" + struct.pack(">Q", size))
    done = 0
    last = time.monotonic()
    for start, end in extents:
        digest.update(struct.pack(">QQ", start, end - start))
        position = start
        while position < end:
            block = os.pread(stream.fileno(), min(1024 * 1024, end - position), position)
            if not block:
                raise ValueError("Image source changed during import; retained partial volume")
            digest.update(block)
            if outgoing is not None:
                if os.pwrite(outgoing.fileno(), block, position) != len(block):
                    raise OSError("Short image write; retained partial volume")
            position += len(block)
            done += len(block)
            now = time.monotonic()
            if now - last >= 5:
                progress(f"{label}: {byte_size(done)} / {byte_size(total)} ({done / total:.0%})")
                last = now
    after = os.fstat(stream.fileno())
    if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
            (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or disk_extents(stream) != (size, extents)):
        raise ValueError("Image source changed during import; retained partial volume")
    progress(f"{label}: {byte_size(done)} / {byte_size(total)} (100%)")
    return digest.hexdigest()


def sync_directory(path):
    directory = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def identifier(value, pattern):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("Invalid storage identifier")
    return value


def atomic(path, data):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save(path, record):
    atomic(path, (json.dumps(record, indent=2) + "\n").encode())


def zfs(*args):
    return subprocess.run(["zfs", *args], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, check=True, timeout=120).stdout.strip()


class Disks:
    def __init__(self, state):
        self.state = Path(state)
        info = self.state.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("Storage records need a private directory owned by this operator")
        self.config = json.loads((self.state / "storage.json").read_text())
        check_state_format(self.config)
        self.dataset = identifier(self.config["dataset"],
                                  r"[a-zA-Z][a-zA-Z0-9_.-]*(/[a-zA-Z0-9_][a-zA-Z0-9_.-]*)+")
        self.owner = identifier(self.config["owner"], r"[0-9a-f]{32}")

    @contextmanager
    def locked(self):
        with (self.state / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @classmethod
    def initialize(cls, state, dataset):
        identifier(dataset, r"[a-zA-Z][a-zA-Z0-9_.-]*(/[a-zA-Z0-9_][a-zA-Z0-9_.-]*)+")
        state = Path(state)
        state.mkdir(mode=0o700, exist_ok=True)
        info = state.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("Refusing unsafe storage record directory")
        sync_directory(state.parent)
        with (state / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            config = state / "storage.json"
            if not config.exists():
                save(config, {"stateFormat": STATE_FORMAT, "dataset": dataset, "owner": uuid.uuid4().hex})
            store = cls(state)
            if store.dataset != dataset:
                raise ValueError("This record directory already manages another dataset")
            for directory in ("images", "machines", "kernels"):
                (state / directory).mkdir(mode=0o700, exist_ok=True)
            sync_directory(state)
            for resource in (dataset, dataset + "/images", dataset + "/machines"):
                if not store.exists(resource):
                    zfs("create", "-o", OWNER + "=" + store.owner,
                        "-o", "mountpoint=none", "-o", "canmount=off",
                        "-o", "compression=lz4", "-o", "sync=standard", resource)
                store.owned(resource, "filesystem")
            if 'stateFormat' not in store.config:
                store.config['stateFormat'] = STATE_FORMAT
                save(config, store.config)
        return store

    def exists(self, dataset):
        parent = dataset.split('@', 1)[0] if '@' in dataset else dataset.rsplit('/', 1)[0]
        return dataset in zfs("list", "-H", "-o", "name", "-t",
                              "filesystem,volume,snapshot", "-d", "1", parent).splitlines()

    def properties(self, *datasets):
        rows = zfs("get", "-H", "-p", "-o", "name,property,value,source",
                   "type," + OWNER + ",origin,volsize,readonly,sync", *datasets)
        result = {name: {} for name in datasets}
        for row in rows.splitlines():
            name, key, value, source = row.split("\t", 3)
            if name not in result or key in result[name]:
                raise ValueError("Unexpected storage property response")
            result[name][key] = (value, source)
        if any(set(values) != {"type", OWNER, "origin", "volsize", "readonly", "sync"}
               for values in result.values()):
            raise ValueError("Incomplete storage property response")
        return result

    def owned(self, dataset, kind, origin=None, properties=None):
        if properties is None:
            rows = zfs("get", "-H", "-p", "-o", "property,value,source",
                       "type," + OWNER + ",origin,volsize,readonly,sync", dataset)
            values = {key: (value, source) for key, value, source in
                      (row.split("\t", 2) for row in rows.splitlines())}
        else:
            values = properties[dataset]
        if values[OWNER] != (self.owner, "local") or values["type"][0] != kind:
            raise ValueError("Refusing unrelated or inherited ownership: " + dataset)
        if origin is not None and values["origin"][0] != origin:
            raise ValueError("Unexpected disk origin: " + dataset)
        if kind == "volume" and values["sync"][0] != "standard":
            raise ValueError("Refusing nonstandard disk flush policy: " + dataset)
        return values

    def image(self, generation, properties=None, *, verify_kernel=True):
        identifier(generation, r"[0-9a-f]{64}")
        record = json.loads((self.state / "images" / (generation + ".json")).read_text())
        if record["id"] != generation or record["pending"] is not None:
            raise ValueError("Image preparation is incomplete")
        if record.get("diskHashScheme", "sha256") not in ("sha256", DISK_HASH_SCHEME):
            raise ValueError("Unknown image disk hash scheme")
        identity = hashlib.sha256((record["diskSha256"] + record["kernelSha256"]).encode()).hexdigest()
        if identity != generation:
            raise ValueError("Image record has changed identity")
        volume = self.dataset + "/images/" + generation
        values = self.owned(volume, "volume", "-", properties)
        if values["readonly"][0] != "on":
            raise ValueError("Prepared image must remain read-only")
        if int(values["volsize"][0]) != record["diskBytes"]:
            raise ValueError("Prepared image volume changed size")
        if verify_kernel:
            with (self.state / "kernels" / generation).open("rb") as kernel:
                digest = hashlib.file_digest(kernel, "sha256").hexdigest()
            if digest != record["kernelSha256"]:
                raise ValueError("Retained kernel differs from this image generation")
        self.owned(volume + "@base", "snapshot", properties=properties)
        return record

    @staticmethod
    def copy_disk(source, device, digest):
        progress("Waiting for image block device")
        subprocess.run(["udevadm", "settle", "--timeout=30",
                        "--exit-if-exists=" + str(device)], check=True, timeout=35)
        with source.open("rb", buffering=0) as incoming, device.open("r+b", buffering=0) as outgoing:
            if not stat.S_ISBLK(os.fstat(outgoing.fileno()).st_mode):
                raise ValueError("Expected a ZFS block device")
            progress("Resetting unpublished image volume without writing zeros")
            fcntl.ioctl(outgoing.fileno(), 0x1277,                    
                        struct.pack("=QQ", 0, os.fstat(incoming.fileno()).st_size))
            actual = sparse_digest(incoming, outgoing=outgoing, label="Copying image data")
            if actual != digest:
                raise ValueError("Image source changed during import; retained partial volume")
            progress("Flushing image data to storage")
            os.fsync(outgoing.fileno())

    def import_image(self, directory):
        progress("Checking prepared image")
        directory = Path(directory)
        disk = directory / "root.img"
        kernel = (directory / "kernel").read_bytes()
        metadata = json.loads((directory / "image.json").read_text())
        capabilities = {flag: metadata.get(flag, 0) for flag in CAPABILITIES}
        for flag, value in capabilities.items():
            if type(value) is not int or value not in (0, 1):
                raise ValueError("Invalid image capability: " + flag + " must be 0 or 1")
        size = disk.stat().st_size
        if (metadata["format"] != "koi-1" or metadata["diskBytes"] != size
                or size < 16384 or size % 16384):
            raise ValueError("Unexpected prepared image format/size")
        kernel_hash = hashlib.sha256(kernel).hexdigest()
        if metadata["kernelSha256"] != kernel_hash:
            raise ValueError("Kernel differs from its prepared image manifest")
        with disk.open("rb", buffering=0) as stream:
            if os.fstat(stream.fileno()).st_size != size:
                raise ValueError("Image size changed before import")
            disk_hash = sparse_digest(stream)
        generation = hashlib.sha256((disk_hash + kernel_hash).encode()).hexdigest()
        path = self.state / "images" / (generation + ".json")
        volume = self.dataset + "/images/" + generation
        snapshot = volume + "@base"
        with self.locked():
            self.owned(self.dataset, "filesystem")
            self.owned(self.dataset + "/images", "filesystem")
            if path.exists():
                record = json.loads(path.read_text())
                if (record["id"] != generation or record["diskSha256"] != disk_hash
                        or record.get("diskHashScheme") != DISK_HASH_SCHEME
                        or record["kernelSha256"] != kernel_hash or record["diskBytes"] != size
                        or record["pending"] not in (None, "copy", "snapshot")):
                    raise ValueError("Image record differs from the prepared input")
                if any(record.get(flag, 0) != value for flag, value in capabilities.items()):
                    raise ValueError("Image capabilities differ from the retained generation")
                if record["pending"] is None:
                    progress("Image generation already imported")
                    return self.image(generation)
            else:
                record = {"id": generation, "diskBytes": size, "kernelSha256": kernel_hash,
                          "diskSha256": disk_hash, "diskHashScheme": DISK_HASH_SCHEME,
                          "pending": "copy", **capabilities}
                atomic(self.state / "kernels" / generation, kernel)
                save(path, record)                                            
            if not self.exists(volume):
                if record["pending"] != "copy":
                    raise ValueError("Copied image volume is missing; refusing an empty replacement")
                progress("Creating thin image volume (" + byte_size(size) + " virtual)")
                zfs("create", "-s", "-V", str(size), "-b", "16K",
                    "-o", OWNER + "=" + self.owner, "-o", "volmode=dev",
                    "-o", "sync=standard", volume)
            values = self.owned(volume, "volume", "-")
            if int(values["volsize"][0]) != size:
                raise ValueError("Unexpected image volume size")
            if record["pending"] == "copy":
                if self.exists(snapshot) or values["readonly"][0] != "off":
                    raise ValueError("Refusing to overwrite a published image")
                self.copy_disk(disk, Path("/dev/zvol") / volume, disk_hash)
                record["pending"] = "snapshot"
                save(path, record)
            progress("Publishing read-only image snapshot")
            zfs("set", "readonly=on", volume)
            if not self.exists(snapshot):
                zfs("snapshot", "-o", OWNER + "=" + self.owner, snapshot)
            self.owned(snapshot, "snapshot")
            record["pending"] = None
            save(path, record)
            progress("Image import complete")
            return self.image(generation)

    def new(self, name, generation, *, trace=None):
        with self.locked():
            record, image, fresh = self._reserve(name, generation, trace=trace)
            return self._clone(record, image, fresh, trace=trace)

    def _reserve(self, name, generation, *, trace=None):
        """Validate image and save intent; caller holds the storage lock."""
        identifier(name, r"[a-z][a-z0-9-]{0,30}")
        identifier(generation, r"[0-9a-f]{64}")
        path = self.state / "machines" / (name + ".json")
        base = self.dataset + '/images/' + generation
        properties = self.properties(self.dataset, self.dataset + '/machines', base, base + '@base')
        if trace is not None:
            trace.mark('provision.disk.properties.end')
        self.owned(self.dataset, "filesystem", properties=properties)
        self.owned(self.dataset + "/machines", "filesystem", properties=properties)
        image = self.image(generation, properties)
        if trace is not None:
            trace.mark('provision.disk.image-validation.end')
        fresh = not path.exists()
        if not fresh:
            record = json.loads(path.read_text())
            if record["name"] != name or record["image"] != generation:
                raise ValueError("Existing machine has a different identity or image")
            if record["pending"] not in (None, "clone"):
                raise ValueError("Machine has another pending operation; refusing disk creation")
            identifier(record["id"], r"[0-9a-f]{32}")
        else:
            record = {"id": uuid.uuid4().hex, "name": name, "image": generation,
                      "pending": "clone"}
            if any(json.loads(other.read_text())['id'] == record['id']
                   for other in path.parent.glob('*.json')):
                raise ValueError('Machine identity is already allocated; no disk was changed')
            save(path, record)
        if trace is not None:
            trace.mark('provision.disk.intent.end')
        return record, image, fresh

    def _clone(self, record, image, fresh, *, trace=None):
        """Finish a reserved disk under the caller's still-held storage lock.

        Managed provisioning may run this in one joined worker. Other workers
        never mutate this machine record or its image; no VMM starts until join.
        """
        path = self.state / "machines" / (record['name'] + ".json")
        volume = self.dataset + "/machines/" + record["id"]
        origin = self.dataset + "/images/" + record['image'] + "@base"
        if trace is not None:
            trace.mark('provision.disk.clone.begin')
        exists = False if fresh else self.exists(volume)
        if trace is not None:
            trace.mark('provision.disk.existence.end')
        if not exists:
            if record["pending"] is None:
                raise ValueError("Retained machine disk is missing; refusing a fresh replacement")
            zfs("clone", "-o", OWNER + "=" + self.owner, "-o", "readonly=off",
                "-o", "volmode=dev", "-o", "sync=standard", origin, volume)
        if trace is not None:
            trace.mark('provision.disk.clone.end')
        values = self.owned(volume, "volume", origin)
        if trace is not None:
            trace.mark('provision.disk.clone-validation.end')
        if values["readonly"][0] != "off":
            raise ValueError("Machine disk unexpectedly became read-only")
        if int(values["volsize"][0]) != image["diskBytes"]:
            raise ValueError("Machine disk changed size outside this fixed profile")
        record["pending"] = None
        save(path, record)
        return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("init")
    setup.add_argument("dataset")
    image = commands.add_parser("image")
    image.add_argument("directory", type=Path)
    machine = commands.add_parser("new")
    machine.add_argument("name")
    machine.add_argument("image")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("This storage preparation tool is operator-run as root")
    if args.command == "init":
        result = Disks.initialize(args.state, args.dataset).config
    elif args.command == "image":
        result = Disks(args.state).import_image(args.directory)
    else:
        result = Disks(args.state).new(args.name, args.image)
    print(json.dumps(result, indent=2))
