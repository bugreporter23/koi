{
  pkgs,
  applicationInputs ? [ ],
  bundle ? {
    version = 1;
    image = "debian-runit";
    architecture = "x86_64";
    boot = "q35-runit-v3";
    payloads = [ ];
    requirements = [ ];
  },
  kernel,
  koiPackage,
}:
let
  repository = import ./debian/repository.nix { inherit pkgs; };
in
{
  inputs = pkgs.runCommand "koi-image-inputs" { nativeBuildInputs = [ pkgs.gnutar ]; } ''
    mkdir -p $out
    ${pkgs.lib.concatMapStringsSep "\n" (input: "cp -rs ${input}/* $out/") applicationInputs}
    cp ${pkgs.writeText "koi-bundle.json" (builtins.toJSON bundle)} $out/bundle.json
    ln -s ${repository} $out/debian
    cp ${./debian/packages.json} $out/packages.json
    cp ${pkgs.incus-lts.src}/internal/server/instance/drivers/agent-loader/incus-agent-setup-linux \
      $out/incus-agent-setup
    ln -s ${kernel} $out/kernel
    mkdir -p metadata/boot metadata/usr/lib
    cp ${kernel}/.config metadata/boot/config-${kernel.version}-koi-q35
    cp -r ${kernel}/lib/modules metadata/usr/lib/
    tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -cf $out/kernel-metadata.tar -C metadata .
    cp ${./boot/first-boot.py} $out/first-boot.py
    cp ${./boot/prepare-agent.sh} $out/prepare-agent.sh
    cp ${./boot/stage1.sh} $out/stage1.sh
    cp ${./boot/prepare-image.sh} $out/prepare-image.sh
  '';
  builder = pkgs.writeShellScriptBin "koi-build-image" ''
    export PATH=${
      pkgs.lib.makeBinPath [
        pkgs.incus-lts.client
        pkgs.qemu
        pkgs.debootstrap
        pkgs.e2fsprogs
        pkgs.gptfdisk
        pkgs.gnutar
        pkgs.coreutils
        pkgs.util-linux
      ]
    }
    exec unshare --mount --net --propagation private ${pkgs.python3}/bin/python3 -I -c 'import sys, runpy; sys.path.insert(0, "${koiPackage.controlSource}"); runpy.run_path("${./build.py}", run_name="__main__")' "$@"
  '';
}
