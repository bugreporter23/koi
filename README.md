# Koi

Persistent per-project VMs on an x86-64 NixOS host, using Cloud Hypervisor,
ZFS clones and a custom Nix/runit guest. Each VM retains its complete writable
disk, including project files, installed packages and services.

Koi is a versioned host controller. The `koi` package provides the `koi-host`
backend; `koi-user-tools` provides the small frontend named `koi`. The module
installs both automatically. The default package and `koi-cli` select the
frontend. Client and host builds must match; using a different frontend fails
before operations and explains how to use the installed matching command.
The frontend uses the fixed host-installed helper, including a separate host
entry for administrative commands requiring ordinary sudo. It never elevates
user-installed code. There is no control daemon or cross-version API.

The image recipe and builder are separate outputs in this repository.
Codex and Claude plugins are bundled by default;
external host plugins are unsupported. Only the checked-in custom guest image
and its fixed virtual hardware profile are supported.

Koi does not require a dotfiles repository, desktop configuration or Nix
channels. Its flake locks its own dependencies; the host provides virtualization,
ZFS support and an operator account.

## Host-local configuration

An existing NixOS configuration can import a separate `koi.nix`. Add
`./koi.nix` to its existing `imports` list and put this in that file:

```nix
{ ... }:
let
  koi = builtins.getFlake "github:bugreporter23/koi/FULL_PUBLIC_COMMIT_SHA";
in
{
  imports = [ koi.nixosModules.default ];
  boot.supportedFilesystems = [ "zfs" ];
  users.mutableUsers = true;
  services.koi = {
    enable = true;
    operator = "alice";
  };
}
```

Replace `FULL_PUBLIC_COMMIT_SHA` with the full public Git revision to install.
Use release 0.3.1 or later for automatic runtime teardown. Keep an existing
`networking.hostId`, or declare a unique eight-digit hexadecimal host ID for ZFS.
The host must enable Nix's `nix-command` and `flakes` experimental features.
Koi preserves the host's selected kernel; the host owner supplies a compatible
ZFS kernel module. Apply with the host's usual `nixos-rebuild switch` command.
Build/import the corresponding image as described below, using that same Git
revision. Updating this setup means changing the pinned revision and rebuilding.

The generator is optional. Neither this pattern nor the module requires a
personal configuration framework or conversion of the whole host to a flake.

## Standalone configuration

Generate a separate locked Koi flake as an ordinary user:

```sh
nix run github:bugreporter23/koi#setup -- \
  --export-config --operator alice --output ./koi-config
```

This writes `flake.nix`, `flake.lock` and `settings.nix` without reading or
changing the host configuration. Keep these files together. A host flake can
add that directory as an input and import its `nixosModules.default`; a
non-flake host can import
`(builtins.getFlake "path:/absolute/path/to/koi-config").nixosModules.default`.
The host must declare the operator, enable mutable users and select a
ZFS-compatible kernel. The generated module enables ZFS, preserves the chosen
kernel and defaults the ZFS host ID from the setup machine. Use `--host-id`
when generating configuration for a different target.

After rebuilding the host configuration, build/import the matching image:

```sh
nix build path:./koi-config#image --out-link koi-image
koi install --image ./koi-image
koi new project
```

The configuration pins the setup invocation's exact immutable source snapshot.
Existing host imports remain the host owner's responsibility. The manual
host-flake integration below is also supported.

## Configure the host

The host needs KVM, ZFS and mutable users for Koi's per-VM service accounts.
Add Koi to your host flake's inputs:

```nix
inputs.koi.url = "github:bugreporter23/koi";
```

Nix records the selected revision in your host's `flake.lock`. Then configure:

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

Once the host module is enabled, build or download the image as an ordinary
user, install its host state and open a VM:

```sh
nix build github:bugreporter23/koi#image --out-link koi-image
koi install --image ./koi-image
koi new project
```

These commands work without a repository checkout or a manually entered Git
hash. Nix uses cached outputs when available and builds missing outputs from
the recipe and its locked dependencies. `koi-image` is a link created by
`nix build` to the finished image directory.

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

To update Koi, update its input from your host flake directory, then run your
usual NixOS rebuild command:

```sh
nix flake update koi
sudo nixos-rebuild switch --flake .
```

Build and import an updated image separately:

```sh
nix build github:bugreporter23/koi#image --out-link koi-image
koi image import ./koi-image
```

The controller rebuild, image build/link and image import are separate steps.
Building the image link leaves installed VM disks alone. Existing VMs keep
their recorded image after an import; new VMs use the selected default.
For a pinned module or flake input, use its selected Koi revision in the image
build reference as well. The unqualified GitHub references above follow the
repository's current default branch.

## Storage capacity

The guest disk's 20 GiB and the default pool file's 32 GiB are virtual capacities.
The pool file starts sparse; imported images copy their data extents into sparse
ZFS volumes without reserving the full disk size. Project disks clone the base
snapshot and share its unchanged blocks. New writes and metadata allocate more
space as needed, including writes made during ordinary boot and guest activity.

The base image, pool metadata and host Nix store still consume real storage.
Different image generations occupy space separately; clone sharing does not
provide a shared guest Nix store or automatic deduplication of later installs.
Deleting guest files does not guarantee the outer pool file will shrink.
The pool's virtual capacity does not reserve host free space: both the host
filesystem and the inner ZFS pool need free space for further writes.

As root, compare allocation with apparent size for the default state directory:

```sh
du -h /var/lib/koi-vm/pool.img
du -h --apparent-size /var/lib/koi-vm/pool.img
zpool list koi-vm
```

`poolSize` chooses capacity when the pool is first created; changing the setting
does not resize an existing pool. Guest disks currently use the fixed 20 GiB
image profile. Persistence and shared base snapshots do not provide backups.

The standalone builder is available for inspection:

```sh
nix build github:bugreporter23/koi#koi-builder --out-link koi-builder
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

Root can also authenticate on the host. Its login stays in root's isolated Koi
state and is imported into the private relay; credentials never enter a VM.

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

`nixos-rebuild switch` applies declared packages, settings, authorization and
system services. A normal Koi controller update preserves VM disks and avoids
restarting the owned network unit and its dependent running guests. Other host
changes can restart services.

Starting with 0.3.1, the Koi service owns runtime teardown as well as startup.
Stopping it, disabling Koi, removing the module import or shutting down the host
stops dependent guest processes and removes owned TAPs, bridge, DNS, inference
workers, service accounts and NAT/firewall rules. The stop hook retains VM disks,
keys, credentials and installation records. Starting the service again restores
its runtime plumbing; guests are started explicitly. Automatic stop never purges
data. NixOS owns the service lifecycle; mutable VM data remains outside the Nix
store, like a database service's retained data directory.

`koi rm project` confirms deletion of one VM disk. `koi uninstall`
stops all VMs and removes their runtime units, TAP interfaces, service accounts,
bridge, DNS and owned NAT/firewall rules while retaining disks, keys and records.
The owned pool and backing file are retained as well. Reinstall
with the same declared settings to resume retained machines.
`koi uninstall --purge` confirms deletion of owned storage and private
state, including an owned file-backed pool and its backing file. An externally
supplied ZFS pool is preserved.

For data deletion, run `koi uninstall --purge` while the module is still enabled,
then disable `services.koi` or remove its import and rebuild. For a pause,
disabling/removing the module and rebuilding cleans up live resources while
keeping disks and installation records; re-enabling it restores the runtime.
Normal `koi uninstall` also removes the installation binding, so resuming after
that explicit action requires `koi install` with the same settings.

The published 0.3.0 module lacks this stop hook: run explicit uninstall before
disabling that version. Cleanup refuses changed/foreign resources and reports
failure rather than deleting them blindly; retained ownership records support
inspection and retry.

Install/import/uninstall require ordinary administrator authorization; selecting
an operator grants only the bounded VM and credential-import operations.
Rerunning `koi install` restarts DNS and dependent guests under the current
lifecycle behavior. After host reboot or retained reinstall, guests are started
explicitly with `new`, `start` or `attach`. After a manual `systemctl stop koi`,
restart the service before operating guests.

Koi pins guest SSH host keys, uses VM-scoped certificates and runs VMMs under
separate confined service identities. Guest IPv4 traffic has NAT access;
the network policy does not restrict outbound destinations. Guests have
passwordless guest sudo. These controls do not establish protection against
hypervisor or kernel vulnerabilities.

## Current constraints

From 0.3.2, retained metadata uses integer `stateFormat: 1`, independently of
the Koi release version. Each build accepts only its exact state format for
VM operations, installation and cleanup. Unsupported or malformed versions
are refused before resource changes; no automatic migrations are provided.
Existing unversioned metadata is defined as format 1, and install writes explicit
version tags. That baseline stays fixed when future formats change. Back up both
the state directory and ZFS storage before an explicit format migration. A Nix
package rollback does not roll back mutable metadata or guest disks. Older
pre-0.3.2 binaries do not enforce this format check.

Koi currently supports one installation and one configured operator on an
x86-64 NixOS host with KVM, ZFS and mutable users. The fixed guest image and
hardware profile, bundled inference adapters and matching host/frontend builds
are intentional constraints. Multiple operators, arbitrary guest distributions,
external host plugins and cross-version client compatibility are unsupported.

Host networking uses imperative owned firewall rules. A host firewall reload
can remove these rules; an administrator can reconcile them with
`/etc/koi/bin/koi-host network-up`. Koi records and later restores the original global IPv4
forwarding value, so another administrator changing that setting during an
installation needs to coordinate it. The default file-backed pool has no
redundancy of its own. Guest disk bytes are not currently reproducible across
separate image assemblies, although dependencies are locked. Vendor subscription
interfaces can change independently of the pinned Koi code.

The source is licensed under [MIT](LICENSE). Bundled third-party packages
retain their own licenses; the guest package selection permits Claude Code's
unfree package.
