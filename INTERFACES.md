# Koi interfaces

The flake supplies x86-64 Linux packages: `default` (compute/workstation CLI),
`control-plane` (daemon, client, gate, host tool and `koi-bundle-check`),
`q35-kernel`, `image-inputs` and `image-builder`. `nixosModules.default` selects the complete CLI;
`nixosModules.control-plane` selects compute management alone.

`lib.bundleContract` is `1`; `lib.bootContract` is `q35-runit-v3`.
`lib.mkImage` supplies explicit application image construction; see [bundle](BUNDLE.md),
[thin guest guarantees](THIN-GUEST.md) and [composition](COMPOSITION.md).

`koi --help` and command help describe options. Compute operations include `list`,
`status`, `network`, `create`, `start`, `stop`, `destroy`, `snapshot`, `restore`,
`endpoint`, `guest-access`, `bind`, `enter` and `exec-guest`. Select a management
destination with `--host`; local use is `local`. `--json` supplies structured output.
`create` requires an explicitly configured image and size and returns its request ID.
VM targets accept a name or immutable ID; exported credentials use immutable IDs.

`koi init --storage-zfs POOL/DATASET` initializes compute.
`koi up NAME --image ALIAS` creates and starts a VM. `koi ssh NAME COMMAND` and
`koi cp SOURCE DESTINATION` use pinned local guest access; copy uses one `VM:path`.
`koi build-image INPUTS` selects an explicit image for future creates.

`bind VM PATH` writes an SSH configuration and private credentials beside it.
It authenticates the guest host key and issues a five-minute client certificate;
repeat binding for fresh access. Established SSH sessions survive expiry.
`guest-access VM --public-key PATH --json` signs a caller-owned public key; the caller retains the private key. A binding requires an agent-delivered
identity and accepting SSH listener. Applications use direct guest connections.

Host configuration is `/etc/koi/host.json`; persistent resources and trust live
under `/var/lib/koi`. The daemon socket is `/run/koi/control.sock` and privileged
operations are serialized. `network --json` reports the Incus-owned local /24,
bridge, MTU and presence. Fleet software may project that prefix while Koi retains local allocation. `koi-host admin-targets apply/check/withdraw` installs, verifies
or removes an owner-scoped management target projection. Host software deployment and fleet signing use their own administrator authority.
