# VM composition and lifecycle

Composition separates compute identity, initial software, running applications
and access. Each object has an owner and an explicit operation that changes it.

| Object | Owner and identity | Change operation |
| --- | --- | --- |
| VM | Koi/Incus; provider plus immutable VM ID; a name selects that object | Explicit create, power, snapshot, restore or destroy request |
| Boot selection | Koi; instance boot configuration and content-addressed matching kernel | Image selection before creation; existing instances retain their selection |
| Image | Koi's base contract plus the bundle's ordered payloads; imported image fingerprint | Explicit build/import, then select an alias for future creates |
| Application instance | Application controller; target VM ID plus its application/session identity | Explicit application install, start, attach, stop or replacement |
| Network/access grant | Compute, fleet or application credential owner for that receiving interface | Explicit authorization, issuance, application or withdrawal |

An image is an initial filesystem recipe. A VM is a persistent compute object.
An application session is a running resource with its own state and lifetime.
Changing a release pin changes selected software intent until its owner applies
it. The current inventory remains authoritative for whether a VM exists.

Build composition has an explicit sequence:

1. Select the supported [base contract](THIN-GUEST.md) and Koi release.
2. Select application payloads and their interface versions.
3. Order their trusted installers and resolve shared file ownership.
4. Build/import the resulting image and select its alias.
5. Create a VM, then activate applications and authorized connections explicitly.

Koi validates the descriptor, boot format and payload input names. The bundle
author owns installer side effects, compatibility and application version checks.
Payload order matters when installers share configuration. Integration recipes
state their prerequisites, files they own, persistent state, activation and
removal procedures. The current builder composes payloads in their stated order.
Resolving shared ownership belongs to the integration recipe.

The image's `koi.bundle` property records initial software intent; `koi.boot` and
`koi.kernel-sha256` record its startup contract. Inspect the running application
for current software facts after any in-guest change. Koi's public inventory and
access commands expose compute facts; the descriptor stays image metadata.

Koi owns the base startup/account/access interfaces. Applications own their
packages, service configuration, sessions and data. River owns fleet projection
and accepted authority. A connection declaration names a requirement; actual
endpoints, scope and credentials are explicitly selected by their owners.
The image descriptor records requirements. Automatic resolution of service
requirements into fleet grants remains a separate integration capability.

Software activation, image publication, application replacement, configuration
changes and authority changes have separate lifetimes. Updating Koi preserves
compute state. Publishing an image selects a template for future guests.
Application replacement follows its controller's state/lifecycle contract.
Restoring a snapshot restores guest disk state; integrations must inspect their
application state afterwards. VM destruction removes its disk and terminates
its running applications. Removing a management client retains the VM.
