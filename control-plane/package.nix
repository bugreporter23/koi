{ pkgs }:
let
  source = pkgs.lib.fileset.toSource {
    root = ./.;
    fileset = ./koi;
  };
  guest = pkgs.lib.fileset.toSource {
    root = ../images/debian-runit;
    fileset = ../images/debian-runit/koi_guest;
  };
  paths = with pkgs; [
    systemd
    openssh
    iproute2
    incus-lts.client
  ];
  command =
    name: module:
    pkgs.writeShellScriptBin name ''
      export PATH="$(dirname -- "$0")":${pkgs.lib.makeBinPath paths}${
        pkgs.lib.optionalString (name == "koi" || name == "koi-host") ":$PATH"
      }
      exec ${pkgs.python3}/bin/python3 -I -c 'import sys, runpy; sys.path.insert(0, "${guest}"); sys.path.insert(0, "${source}"); runpy.run_module("koi.${module}", run_name="__main__")' "$@"
    '';
in
pkgs.symlinkJoin {
  name = "koi-control-plane";
  paths = [
    (command "koi" "client")
    (command "koi-admin" "admin")
    (command "koi-daemon" "daemon")
    (command "koi-gate" "gate")
    (command "koi-host" "host")
    (command "koi-bundle-check" "bundle")
  ];
  passthru.controlSource = source;
  postBuild = ''
    mkdir -p "$out/share/zsh/site-functions"
    "$out/bin/koi" completions zsh > "$out/share/zsh/site-functions/_koi"
  '';
  passthru.guestSource = guest;
  meta.mainProgram = "koi";
}
