{ pkgs }:
[
  (import ./codex { inherit pkgs; })
  (import ./claude { inherit pkgs; })
]
