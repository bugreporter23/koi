{ pkgs }:
let
  base = builtins.fromJSON (builtins.readFile ./base.json);
  packages = builtins.fromJSON (builtins.readFile ./packages.json);
  debs = pkgs.linkFarm "koi-debian-packages" (
    map (p: {
      inherit (p) name;
      path = pkgs.fetchurl { inherit (p) url sha256; };
    }) packages
  );
in
pkgs.runCommand "koi-image-inputs" { nativeBuildInputs = [ pkgs.gnutar ]; } ''
  mkdir -p $out
  ln -s ${pkgs.fetchurl base.artifacts."incus.tar.xz"} $out/incus.tar.xz
  ln -s ${pkgs.fetchurl base.artifacts."disk.qcow2"} $out/disk.qcow2
  tar -chf $out/packages.tar -C ${debs} .
  cp ${./base.json} $out/base.json
  cp ${./packages.json} $out/packages.json
''
