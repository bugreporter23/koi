{ pkgs }:
let
  packages = builtins.fromJSON (builtins.readFile ./packages.json);
  debs = pkgs.linkFarm "koi-debian-packages" (
    map (package: {
      inherit (package) name;
      path = pkgs.fetchurl {
        inherit (package) url sha256;
        name = package.name;
      };
    }) packages
  );
in
pkgs.runCommand "koi-debian-repository"
  {
    nativeBuildInputs = [
      pkgs.python3
      pkgs.dpkg
    ];
  }
  ''
    mkdir -p $out/pool $out/dists/trixie/main/binary-amd64
    ln -s ${debs}/* $out/pool/
    python3 ${./repository.py} $out
  ''
