# Koi

Persistent per-project VMs on an x86-64 NixOS host, using Cloud Hypervisor,
ZFS clones and a custom Nix/runit guest. Each VM retains its complete writable
disk, including project files, installed packages and services.

Koi is a versioned host controller. The image recipe and builder are separate
outputs in this repository. Codex and Claude plugins are bundled by default;
external host plugins are unsupported. Only the checked-in custom guest image
and its fixed virtual hardware profile are supported.

## Configure the host

The host needs KVM, ZFS and mutable users for Koi's per-VM service accounts.
Add this flake as `koi` in your host flake, then configure:

```nix
imports = [ koi.nixosModules.default ];
boot.supportedFilesystems = [ "zfs" ];
users.mutableUsers = true;
services.koi = {
  enable = true;
  operator = "alice"; # Existing local user authorized to operate VMs.
  # poolSize = "64GiB";       # Default: 32 GiB sparse file-backed pool.
  # dataset = "tank/koi";     # Optional unused namespace in an existing pool.
  # subnet = "10.201.0.0/24"; # Must not overlap a host route.
};
```

Apply your host configuration with `nixos-rebuild switch`. The module installs
the command, declared settings, fixed sudo helper and network unit. The
default installation uses an owned sparse file-backed ZFS pool; a ZFS root
filesystem is unnecessary.

## Build, import and use

From a checkout of this repository, build the image as an ordinary user:

```sh
nix build .#image --out-link koi-image
sudo koi install
sudo koi image import ./koi-image
koi new project
```

`new` opens a guest shell as `dev` in the project directory. Exit the shell
to return to the host; the VM keeps running. Host commands include:

```sh
koi attach project
koi stop project
koi start project
koi attach project pwd
```

`install` creates host state and networking. Image import copies finished
disk and kernel bytes into owned storage. Koi never executes the image builder.
Import selects the default generation for new VMs. Existing machines retain
their recorded image, kernel and writable disk.

An updated controller is installed through the host's NixOS rebuild. A kernel
or guest userspace update requires a separate image build and import. See
[supported image expectations](images/FORMAT.md) for this boundary.

The standalone builder is available for inspection:

```sh
nix build .#koi-builder --out-link koi-builder
./koi-builder/bin/koi-build-image /path/to/new-image-directory
```

The output directory must be new and its parent writable. Construction uses
an ordinary staging tree, a guest-local Nix database and `fakeroot` metadata
consumed by `mkfs.ext4 -d`. It produces one sparse GPT/ext4 disk. Filesystem
identifiers and timestamps are generated during assembly, so separate image
builds do not currently promise identical disk bytes.

## Bundled inference

Run interactive login on the host as the configured operator:

```sh
koi login codex
koi login claude
koi attach project codex
koi attach project claude
```

Host subscription credentials stay in private host state. The guest uses
provided loopback settings and a runit service forwarding requests over vsock
to its dedicated host relay. The image builder installs these components from
the [bundled plugins](adapters/README.md). Creating a VM does not perform login.
Vendor subscription interfaces may change independently of Koi.

On a remote or headless host, forward Codex's device-login option:

```sh
koi login codex --device-auth
```

Open the printed URL in your browser and enter the displayed code. Options
after the provider name are passed to its native host login command.

New inference-capable VMs receive a grant by default:

```sh
koi revoke project
koi grant project
```

Revocation stops that VM's relay and active streams. It cannot undo requests
already accepted upstream. Existing disks without the guest forwarding setup
require a new image and VM to use inference.

## Retention and removal

`koi rm project` confirms deletion of one VM disk. `sudo koi uninstall`
removes runtime integration while retaining disks, keys and records. Reinstall
with the same declared settings to resume retained machines.
`sudo koi uninstall --purge` confirms deletion of owned storage and private
state. Uninstall before removing `services.koi` from the host configuration.

Koi pins guest SSH host keys, uses VM-scoped certificates and runs VMMs under
separate confined service identities. Guest IPv4 traffic has NAT access;
the network policy does not restrict outbound destinations. Guests have
passwordless guest sudo. These controls do not establish protection against
hypervisor or kernel vulnerabilities.

The source is licensed under [MIT](LICENSE). Bundled third-party packages
retain their own licenses; the guest package selection permits Claude Code's
unfree package.
