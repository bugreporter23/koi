# Working with Koi source

Koi's controller, bundled plugins and separate Nix/runit image recipe have
distinct owners. Read [README.md](README.md), [image expectations](images/FORMAT.md)
and [plugin ownership](adapters/README.md) before proposing changes.

The public repository is a generated source snapshot. Private design, tests
and release evidence are maintained separately. Report findings through the
private ticket endpoint advertised in `PUBLICATE.json` when you have access.
Use Publicate's ticket workflow; ticket content remains private.

Build the controller with `nix build .#koi`, the separate builder with
`nix build .#koi-builder`, and the disk image with `nix build .#image`.
Image assembly needs no host root privileges. Host installation, NixOS
configuration changes, VM acceptance and subscription login require an
operator-selected host and operator-run steps.

Keep external host plugin loading unsupported. Image import consumes finished
artifacts; the controller must not execute builder code. An updated image
must preserve existing machines' recorded generations and writable disks.
