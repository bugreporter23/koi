{
  pkgs,
  guest,
  plugins,
  nixpkgs,
}:
let
  runit = pkgs.pkgsStatic.runit;
  system = guest.buildEnv {
    name = "koi-system-tools";
    paths = pkgs.lib.unique (
      with guest;
      [
        bashInteractive
        coreutils
        util-linux
        procps
        iproute2
        openssh
        sudo
        eudev
        acpid
        chrony
        gitMinimal
        curl
        gnutar
        gzip
        xz
        gnugrep
        gnused
        findutils
        kmod
        glibc.bin
        glibc.getent
      ]
      ++ [ runit ]
      ++ pkgs.lib.concatMap (plugin: plugin.guest.systemPackages) plugins
    );
    pathsToLink = [
      "/bin"
      "/sbin"
    ];
  };
  tools = guest.buildEnv {
    name = "koi-agent-tools";
    paths = [ guest.nix ] ++ pkgs.lib.concatMap (plugin: plugin.guest.packages) plugins;
  };
  closure = guest.closureInfo {
    rootPaths = [
      system
      tools
      guest.bash
      guest.pam
      guest.cacert
    ];
  };
  services = pkgs.lib.foldl' (acc: plugin: acc // plugin.guest.services) { } plugins;
  pam = "${guest.pam}/lib/security";
in
pkgs.runCommand "koi-userspace" { } ''
  mkdir -p "$out/home" "$out/services"
  ${pkgs.lib.concatStringsSep "\n" (
    pkgs.lib.mapAttrsToList (name: script: ''
      cp ${script} "$out/services/${name}"
    '') services
  )}
  cp ${closure}/store-paths "$out/store-paths"
  cp ${closure}/registration "$out/registration"
  ${pkgs.lib.concatMapStringsSep "\n" (plugin: ''
    cp -r ${plugin.guest.home}/. "$out/home/"
  '') plugins}
  cat > "$out/manifest.json" <<'EOF'
  ${builtins.toJSON {
    profile = "${tools}";
    systemProfile = "${system}";
    nixStore = "${guest.nix}/bin/nix-store";
    bash = "${guest.bash}";
    ldd = "${guest.glibc.bin}/bin/ldd";
    sudo = "${guest.sudo}/bin/sudo";
    pamModules = pam;
    pamHelper = "${guest.pam}/bin/unix_chkpwd";
    caBundle = "${guest.cacert}/etc/ssl/certs/ca-bundle.crt";
    sshd = "${guest.openssh}/bin/sshd";
    udevd = "${guest.eudev}/bin/udevd";
    entryPrograms = [
      "${runit}/bin/runit"
      "${runit}/bin/runit-init"
      "${runit}/bin/runsvdir"
      "${runit}/bin/runsv"
      "${guest.openssh}/bin/sshd"
      "${guest.openssh}/libexec/sshd-session"
      "${guest.openssh}/libexec/sshd-auth"
      "${guest.openssh}/bin/ssh-keygen"
      "${guest.bashInteractive}/bin/bash"
      "${guest.procps}/bin/sysctl"
      "${guest.acpid}/bin/acpid"
      "/usr/local/libexec/busybox"
    ];
    servicePrograms = [
      "${guest.eudev}/bin/udevd"
      "${guest.eudev}/bin/udevadm"
      "${guest.chrony}/bin/chronyd"
      "${guest.nix}/bin/nix-daemon"
    ];
    versionPrograms = [
      {
        binary = "${guest.nix}/bin/nix";
        expected = "nix (Nix) ${guest.nix.version}";
      }
    ]
    ++ map (plugin: plugin.guest.versionProgram) plugins;
    nixpkgsRev = nixpkgs.rev;
    versions = {
      nix = guest.nix.version;
      openssh = guest.openssh.version;
      pam = guest.pam.version;
      runit = runit.version;
      eudev = guest.eudev.version;
    }
    // builtins.listToAttrs (
      map (plugin: {
        name = plugin.name;
        value = plugin.package.version;
      }) plugins
    );
  }}
  EOF
''
