# Thin guest integration contract

The supported base is Debian 13/amd64 with boot contract `q35-runit-v3`.
`lib.mkImage` supplies this base; the bundle descriptor uses version `1`.
A change to a required boot interface needs an explicit contract change.
Integrations select their Koi release and application versions together.

| Boundary | Provided by the base |
| --- | --- |
| Machine | Headless Incus/Q35 on ordinary unencrypted x86-64 KVM; virtio networking, SCSI disk, serial, vsock, RNG and agent shares |
| Boot | Pinned matching Linux kernel, direct ext4 root at `/dev/sda1`, runit startup and supervision |
| Disk | Sparse 20 GiB GPT/ext4 template; larger selected disks grow the root partition/filesystem during guest startup |
| Account | First boot creates `dev`, UID/GID `1000`, home `/home/dev`, login shell `/bin/bash`, and passwordless in-guest sudo |
| Identity | Fresh guest machine ID and Ed25519 SSH host key; provider seed carries name, immutable VM ID and public guest-access CA |
| Access | OpenSSH on port 22; login as `dev` through Koi certificates, host-key pinning and the immutable-ID principal |
| Network | `eth0` uses DHCP on Koi's Incus bridge; the provider reserves its allocated IPv4 address; guest DNS comes from DHCP |
| Services | Supervised SSH, Incus agent, DHCP, Chrony and ACPI shutdown handling; udev and startup sysctl setup |
| Runtime tools | Debian shell/core utilities, Python 3, SSH/SFTP, sudo, Git, curl, tmux, apt, CA certificates and filesystem/network utilities |
| Nix storage | Writable `/nix`, assigned to `dev` at initialization; application closure content is supplied by bundle payloads |
| Mutable state | Persistent guest disk and `/home/dev`; `/run` is temporary and `/tmp` is cleared during boot |

The exact system package names, URLs and hashes are in
[the pinned Debian manifest](images/debian-runit/debian/packages.json).
Pinned build inputs and sparse disk capacity describe different costs. Building
inputs can compile a kernel; filesystem assembly requires root and offline
Debian packages. Ordinary guest startup runs the selected kernel and runit setup.

Selected guest disks must accommodate the prepared 20 GiB template.

The host supplies KVM, Incus, its supported agent executable share, native ZFS,
an imported parent pool and local networking. Image publication places the
matching kernel under `/usr/share/qemu/koi/DIGEST/kernel`. Creation verifies that
kernel and writes the selected boot configuration into the Incus instance.

First boot consumes the provider seed and records `/var/lib/koi/initialized`.
Later boots retain the account, trust configuration, machine ID, SSH host key
and application files. `/var/lib/koi/disk-sectors` records successful disk growth.
Application provisioning and startup must respect these base-owned files,
`/etc/runit/1`, the agent setup, Koi SSH configuration and the reserved account.
Applications may add their own runit services and selected account configuration
through deliberate integration, preserving the base's startup/access interfaces.

The bundle supplies amux/Codex, personal configuration, application service
activation, transcripts and other application state. A usable Nix installation,
closure registration and GC roots are also bundle responsibilities. The base's
writable store directory supplies the filesystem boundary for those installations.
Each application documents its prerequisites, state paths and lifecycle.

The guest account can become root, install software and retain SSH access.
Certificate expiry bounds future certificate authentication; established sessions
and deliberately installed access have their own lifetimes. Fleet projection,
remote service reachability and application credentials require their owners'
explicit policy and activation.

See [bundle construction](BUNDLE.md) and [VM composition](COMPOSITION.md).
