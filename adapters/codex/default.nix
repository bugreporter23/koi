{ pkgs }:
let
  targetVersion = "0.161.0";
in
assert pkgs.codex.version == targetVersion;
{
  name = "codex";
  inherit targetVersion;
  package = pkgs.codex;
  hostModule = ./relay.py;
  guest = (import ../guest.nix { inherit pkgs; }) // {
    packages = [ pkgs.codex ];
    home = ./home;
    versionProgram = {
      binary = "${pkgs.codex}/bin/codex";
      expected = "codex-cli ${targetVersion}";
    };
  };
}
