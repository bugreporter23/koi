{
  pkgs,
  control,
  imageBuilder,
}:
let
  cli = pkgs.lib.fileset.toSource {
    root = ./.;
    fileset = pkgs.lib.fileset.unions [
      ./cli.py
      ./workstation.py
    ];
  };
  launcher = pkgs.writeText "koi-cli" ''
    #!${pkgs.runtimeShell}
    export PATH=@package@/bin:${
      pkgs.lib.makeBinPath [
        pkgs.openssh
        pkgs.nix
      ]
    }:$PATH
    export KOI_IMAGE_BUILDER=${imageBuilder}/bin/koi-build-image
    exec ${pkgs.python3}/bin/python3 -I -c 'import sys, runpy; sys.path.insert(0, "${control.controlSource}"); sys.path.insert(0, "${cli}"); runpy.run_path("${cli}/cli.py", run_name="__main__")' "$@"
  '';
in
pkgs.runCommand "koi" { meta.mainProgram = "koi"; } ''
  mkdir -p "$out/bin"
  for command in koi-admin koi-daemon koi-gate koi-host koi-bundle-check; do
    ln -s ${control}/bin/$command "$out/bin/$command"
  done
  cp ${launcher} "$out/bin/koi"
  substituteInPlace "$out/bin/koi" --replace-fail @package@ "$out"
  chmod +x "$out/bin/koi"
  mkdir -p "$out/share/zsh/site-functions"
  "$out/bin/koi" completions zsh > "$out/share/zsh/site-functions/_koi"
''
