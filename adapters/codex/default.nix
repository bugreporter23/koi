{ pkgs }:
{
  name = "codex";
  package = pkgs.codex;
  hostModule = ./relay.py;
  guest = (import ../guest.nix { inherit pkgs; }) // {
    packages = [ pkgs.codex ];
    home = ./home;
    versionProgram = {
      binary = "${pkgs.codex}/bin/codex";
      expected = "codex-cli ${pkgs.codex.version}";
    };
  };
}
