# Koi image bundle contract

Contract version **1** targets `x86_64` Debian 13, boot contract `q35-runit-v3`,
the `dev` guest account, SSH certificate access and a copied Nix store. Koi owns
boot preparation, kernel matching, image import and guest identity. A bundle owns
application packages, their configuration and application state.

`lib.mkImage { applicationInputs = [ INPUTS ]; bundle = DESCRIPTOR; }` returns
`inputs` and `builder`. It uses Koi's pinned base image dependencies. `INPUTS`
directories contain named archives/installers and any additional application files.
Empty payloads produce an application-free image. The descriptor has exactly:

```json
{
  "version": 1,
  "image": "working-environment",
  "architecture": "x86_64",
  "boot": "q35-runit-v3",
  "payloads": [
    {"archive": "application-runtime.tar", "installer": "application-install.py"}
  ],
  "requirements": []
}
```

`koi-bundle-check FILE` validates a descriptor; `-` reads JSON from stdin.
Unsupported versions, boot contracts, architectures, fields or relationships fail.
Payload filenames are unique local basenames. Archives are tar files, extracted
into the guest root. Installers run in descriptor order as trusted host Python
processes with two arguments: the writable guest root and the input directory.
They may modify guest files and run explicit preparation in that root. Failures
stop image assembly; payload selection belongs to the trusted build configuration.

The resulting Incus image retains `koi.bundle`, `koi.boot` and the content-addressed
kernel digest. The descriptor records initial software intent. Current software facts require inspection of the running guest. Image import selects its `image` alias for
future creation. Existing disks and processes retain their state.

Connection requirements are declarations, separate from access grants. Each entry
has `name`, `direction` (`guest-to-service`), `service` (a symbolic lowercase name)
and `protocol` (`tcp`). Real endpoints and approved network scope belong to the
operator and fleet policy. Routing, firewall changes and credentials require explicit operator policy.
Use Koi's current immutable guest identity, `network`, `bind` and `guest-access`
facts when composing access. Applications own their grant and process lifetimes.
