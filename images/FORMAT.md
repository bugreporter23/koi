# Supported image expectations

Koi supports the custom Nix/runit image in `images/nix-runit/`. This document
records the existing builder/launcher boundary; it is not a general image or
external plugin API. Other distributions and arbitrary hardware are unsupported.

The separate builder produces one directory:

- `root.img`: a sparse raw GPT disk with 20 GiB virtual capacity and an ext4
  root partition at sector 2048. Its physical storage grows with allocated data.
- `kernel`: the matching uncompressed x86-64 Linux ELF with its PVH entry point.
- `kernel.config`: the build configuration for inspection.
- `image.json`: `format: "koi-1"`, the exact `diskBytes`, `kernelSha256`, guest
  identity, root device, tool versions and existing capability flags.

The current flags are `bootTrace`, `sshCertificates`, `staticNetworking` and
`inferenceRelay`, each set to `1`. Flags must be integer `0` or `1`; absent flags
default to `0` for older images. Koi records them with the imported generation
and rejects reimported metadata that contradicts the retained capabilities.
Import verifies the existing format/size and kernel hash, then hashes and copies
only the disk's reported data extents into a thin ZFS volume. Holes are neither
read nor hashed. The digest includes virtual size, extent offsets, lengths and
data; allocation layout therefore participates in generation identity. Existing
generations retain their original identity. An interrupted import can resume
snapshot publication only while its already copied volume still exists; a
missing copied volume is never replaced with a blank image. The input filesystem
must support
Linux `SEEK_DATA`/`SEEK_HOLE`; ranges it reports as data are processed even if
they contain zeros. Import reports data sizes, progress and storage flushes.
It never executes a builder or
script from the directory. The flags describe supported guest behavior; they do
not establish that arbitrary image contents are trustworthy or bootable.

The launcher provides the existing Cloud Hypervisor virtual machine: the root
on virtio block `/dev/vda1`, a read-only public ext4 seed on `/dev/vdb`, virtio
networking and vsock. The image implements the fixed seed initialization,
static network setup, `dev` SSH/certificate access and readiness report on vsock
port 1024. Guest init handles ACPI poweroff and preserves state and host keys.
The bundled inference guest service forwards loopback port 8400 over vsock to
the host relay on port 8400. The builder installs that service from the plugins'
provided guest components.

Build and import are separate:

```sh
nix build .#image --out-link koi-image
sudo koi install
sudo koi image import ./koi-image
```

Image construction is an ordinary unprivileged Nix build. Koi is a separately
versioned system package. A new kernel or userspace means building and importing
a new image, without a NixOS rebuild. New VMs use the selected generation;
existing VM records, kernels and writable disks retain theirs.

The builder places only reported data extents into the fresh GPT disk, without
scanning partition holes. Raw files in Nix outputs still undergo Nix's own
logical-content hashing/serialization, which can scan the virtual size despite
sparse physical allocation. The current image output is not a compact archive.
