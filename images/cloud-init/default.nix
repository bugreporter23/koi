{ pkgs, koiPackage }:
{
  inputs = import ./inputs.nix { inherit pkgs; };
  builder = pkgs.writeShellScriptBin "koi-build-cloud-image" ''
    export PATH=${pkgs.lib.makeBinPath [ pkgs.incus-lts.client ]}
    exec ${pkgs.python3}/bin/python3 -I -c 'import sys, runpy; sys.path.insert(0, "${koiPackage.controlSource}"); sys.path.insert(0, "${./.}"); runpy.run_module("koi_cloud_image", run_name="__main__")' "$@"
  '';
}
