{ pkgs, plugins }:
let
  version = pkgs.lib.removeSuffix "\n" (builtins.readFile ./VERSION);
  traceLauncher = pkgs.pkgsStatic.stdenv.mkDerivation {
    pname = "koi-trace-exec";
    inherit version;
    src = ./trace-exec.c;
    dontUnpack = true;
    buildPhase = "$CC -std=c11 -Os -Wall -Wextra -Werror -o koi-trace-exec $src";
    installPhase = ''mkdir -p "$out/bin"; cp koi-trace-exec "$out/bin/"'';
  };
  source = pkgs.runCommand "koi-source-${version}" { nativeBuildInputs = [ pkgs.python3 ]; } ''
    mkdir -p "$out/koi/inference_adapters" "$out/adapters"
    cp ${./.}/*.py "$out/koi/"
    cp ${./VERSION} "$out/koi/VERSION"
    cp ${./inference_adapters}/*.py "$out/koi/inference_adapters/"
    ${pkgs.lib.concatMapStringsSep "\n" (plugin: ''
      mkdir -p "$out/adapters/${plugin.name}"
      cp ${plugin.hostModule} "$out/adapters/${plugin.name}/relay.py"
    '') plugins}
    python3 -m compileall -q --invalidation-mode=checked-hash "$out"
  '';
  clientId = builtins.hashString "sha256" (builtins.unsafeDiscardStringContext (toString source));
  clientSource = pkgs.writeTextDir "client.py" (builtins.readFile ./client.py);
  userTools =
    (pkgs.writeScriptBin "koi" ''
      #!${pkgs.python3}/bin/python3 -I
      import sys
      sys.path.insert(0, "${clientSource}")
      import client
      client.main("${clientId}", "${version}")
    '').overrideAttrs
      (old: {
        name = "koi-user-tools-${version}";
      });
  backend = pkgs.writeScriptBin "koi-host" ''
    #!${pkgs.python3}/bin/python3 -I
    import os
    import sys
    os.environ["KOI_EXECUTABLE"] = os.path.realpath(__file__)
    os.environ["PATH"] = "${
      pkgs.lib.makeBinPath (
        with pkgs;
        [
          python3
          cloud-hypervisor
          openssh
          socat
          nix
          systemd
          e2fsprogs
          coreutils
          iproute2
          dnsmasq
          iptables
          shadow
          sudo
        ]
        ++ [ traceLauncher ]
        ++ map (plugin: plugin.package) plugins
      )
    }:" + os.environ.get("PATH", "")
    sys.path.insert(0, "${source}/koi")
    import runtime
    runtime.main("${clientId}")
  '';
  nativeLogin = pkgs.writeScriptBin "koi-login" ''
    #!${pkgs.python3}/bin/python3 -I
    import os
    import sys
    os.environ["PATH"] = "${
      pkgs.lib.makeBinPath (map (plugin: plugin.package) plugins)
    }:" + os.environ.get("PATH", "")
    sys.path.insert(0, "${source}/koi")
    import login
    login.main()
  '';
in
{
  host = pkgs.symlinkJoin {
    name = "koi-${version}";
    paths = [
      backend
      nativeLogin
    ];
    passthru = { inherit userTools clientId; };
  };
  inherit userTools;
  tools = pkgs.symlinkJoin {
    name = "koi-tools";
    paths = with pkgs; [
      cloud-hypervisor
      socat
      dnsmasq
      iproute2
      e2fsprogs
      openssh
    ];
  };
}
