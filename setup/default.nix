{ pkgs, source }:
pkgs.writeShellApplication {
  name = "koi-setup";
  runtimeInputs = [ pkgs.python3 pkgs.nix ];
  text = ''
    export KOI_SETUP_SOURCE=${source}
    export KOI_SETUP_ENTRY="$0"
    exec python3 ${./setup.py} "$@"
  '';
}
