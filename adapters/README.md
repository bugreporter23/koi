# Bundled Koi plugins

Codex and Claude are a fixed, reviewed set bundled with the Koi release. The
controller loads only their checked-in host modules; it does not discover or
execute external host plugins. Host modules process private credentials and are
trusted as Koi code. An external host plugin interface is explicitly unsupported.

Each `default.nix` provides the plugin's host module and login package, plus its
guest packages, home configuration and version check. `guest.nix` supplies the
shared guest transport dependencies and forwarding service. The fixed registry
is `adapters/default.nix`.

The separate image builder consumes these guest components. It installs their
packages and configuration and registers the forwarding service with guest
runit. Koi's launcher imports the finished image; it does not configure the guest
by running plugin installation hooks on the host.

The guest agents call loopback port 8400. The shared guest service forwards that
connection over vsock to the per-VM host relay. Subscription credentials and
refresh remain on the host. The native configuration contains only relay
placeholders, never account tokens.

The flake includes both plugins by default. Bundling their host login binaries
in the system package is intentional; the guest kernel, userspace payload and
image builder are separate outputs.
