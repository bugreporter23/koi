{ pkgs }:
{
  name = "claude";
  package = pkgs.claude-code;
  hostModule = ./relay.py;
  guest = (import ../guest.nix { inherit pkgs; }) // {
    packages = [ pkgs.claude-code ];
    home = ./home;
    versionProgram = {
      binary = "${pkgs.claude-code}/bin/claude";
      expected = "${pkgs.claude-code.version} (Claude Code)";
    };
  };
}
