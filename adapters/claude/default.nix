{ pkgs }:
let
  targetVersion = "2.1.292";
in
assert pkgs.claude-code.version == targetVersion;
{
  name = "claude";
  inherit targetVersion;
  package = pkgs.claude-code;
  hostModule = ./relay.py;
  guest = (import ../guest.nix { inherit pkgs; }) // {
    packages = [ pkgs.claude-code ];
    home = ./home;
    versionProgram = {
      binary = "${pkgs.claude-code}/bin/claude";
      expected = "${targetVersion} (Claude Code)";
    };
  };
}
