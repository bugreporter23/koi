# Koi

Koi manages local Incus virtual machines on native ZFS. It owns immutable guest
identity, local networking, SSH trust, storage admission and image construction.
Applications supply their packages and attachment commands separately.

Requires x86-64 Linux, Incus with KVM, native ZFS and an existing parent dataset.
The NixOS module selects software; host administration enables Incus and grants
local management permission. Koi keeps state under `/var/lib/koi`.

```nix
{
  imports = [ koi.nixosModules.default ];
  virtualisation.incus.enable = true;
}
```

After activating the host configuration, initialize compute and select an image:

```sh
koi init --storage-zfs POOL/koi
inputs=$(nix build .#image-inputs --no-link --print-out-paths)
koi build-image "$inputs"
koi up dev --image debian-runit
koi ssh dev uname -a
```

`image-inputs` supplies an application-free Debian 13/runit image with the matching
Q35 direct-boot kernel. Image construction needs trusted root administration;
`koi build-image INPUTS --output DIRECTORY` creates standalone artifacts.
A selected application bundle can supply explicit payloads through
[the bundle contract](BUNDLE.md).

```sh
koi list --json
koi stop dev
koi snapshot dev checkpoint
koi start dev
koi bind dev /tmp/dev-ssh.conf
koi exec-guest dev -- uname -a
```

Stop before snapshot or restore. Destruction deletes the guest disk. Replacing
software preserves guest identity and data; selecting a new image affects future
creates. Disabling Koi withdraws its owned hooks and retains compute resources.
Removing resources is a separate administrator operation.

[Interfaces](INTERFACES.md) describes packages, configuration and access.
Koi's original code is [MIT licensed](LICENSE).
