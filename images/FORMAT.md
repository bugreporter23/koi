# Supported image expectations

Koi supports the custom Nix/runit image in `images/nix-runit/`. This document
records the existing builder/launcher boundary; it is not a general image or
external plugin API. Other distributions and arbitrary hardware are unsupported.

The separate builder produces one directory:

- `root.img`: a 20 GiB raw GPT disk with an ext4 root partition at sector 2048.
- `kernel`: the matching uncompressed x86-64 Linux ELF with its PVH entry point.
- `kernel.config`: the build configuration for inspection.
- `image.json`: `format: "koi-1"`, the exact `diskBytes`, `kernelSha256`, guest
  identity, root device, tool versions and existing capability flags.

The current flags are `bootTrace`, `sshCertificates`, `staticNetworking` and
`inferenceRelay`, each set to `1`. Koi records them with the imported generation.
Import verifies the existing format/size and kernel hash, hashes the disk, and
copies disk/kernel bytes into owned storage. It never executes a builder or
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
