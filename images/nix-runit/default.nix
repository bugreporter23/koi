{
  pkgs,
  guest,
  plugins,
  guestNixpkgs,
}:
let
  guestTools = import ./userspace.nix {
    inherit pkgs guest plugins;
    nixpkgs = guestNixpkgs;
  };
  ready = pkgs.pkgsStatic.stdenv.mkDerivation {
    pname = "koi-ready";
    version = "1";
    src = ./ready.c;
    dontUnpack = true;
    buildPhase = ''
      $CC -std=c11 -Os -Wall -Wextra -Werror -o koi-ready $src
      $CC -std=c11 -Os -Wall -Wextra -Werror -o koi-prefetch ${./prefetch.c}
    '';
    installPhase = ''mkdir -p "$out/bin"; cp koi-ready koi-prefetch "$out/bin/"'';
  };
  originalKernel = import ./q35-kernel.nix { inherit pkgs; };
  kernel = originalKernel.overrideAttrs (old: {
    pname = "koi-kernel";
    configurePhase = old.configurePhase + ''
      scripts/config --enable VIRTIO_BLK --enable VIRTIO_BALLOON \
        --enable PAGE_REPORTING --enable OVERLAY_FS --enable BRIDGE \
        --enable VETH --enable NF_TABLES --enable NF_CONNTRACK --enable NF_NAT
      scripts/config --enable NAMESPACES --enable USER_NS --enable PID_NS \
        --enable NET_NS --enable IPC_NS --enable UTS_NS --enable CGROUPS \
        --enable MEMCG --enable CGROUP_PIDS --enable CPUSETS \
        --enable CGROUP_SCHED --enable FAIR_GROUP_SCHED \
        --enable CFS_BANDWIDTH --enable SECCOMP --enable SECCOMP_FILTER \
        --enable BPF --enable BPF_SYSCALL --enable CGROUP_BPF \
        --enable NETFILTER_ADVANCED --enable NETFILTER_XTABLES --enable NFT_CT --enable NFT_MASQ \
        --enable NFT_NAT --enable NFT_COMPAT --enable BRIDGE_NETFILTER \
        --enable IP_NF_IPTABLES --enable NETFILTER_XT_MATCH_ADDRTYPE \
        --enable NETFILTER_XT_MATCH_CONNTRACK
      make olddefconfig
      for option in VIRTIO_BLK VIRTIO_PCI VIRTIO_NET VIRTIO_VSOCKETS \
        VIRTIO_BALLOON PAGE_REPORTING EXT4_FS OVERLAY_FS BRIDGE VETH \
        USER_NS PID_NS NET_NS IPC_NS UTS_NS CGROUPS MEMCG CGROUP_PIDS \
        CPUSETS CFS_BANDWIDTH SECCOMP_FILTER BPF_SYSCALL CGROUP_BPF \
        NF_TABLES NF_CONNTRACK NF_NAT NFT_COMPAT IP_NF_IPTABLES; do
        grep -qx "CONFIG_$option=y" .config
      done
    '';
  });
  bootKernel =
    pkgs.runCommand "koi-pvh-kernel"
      {
        nativeBuildInputs = with pkgs; [
          gnutar
          lz4
          binutils
        ];
      }
      ''
        tar -xOf ${pkgs.linux.src} --wildcards '*/scripts/extract-vmlinux' > extract-vmlinux
        mkdir "$out"
        sh extract-vmlinux ${kernel}/bzImage > "$out/vmlinux"
        readelf -n "$out/vmlinux" | grep -q Xen
        cp ${kernel}/.config "$out/.config"
        ln -s ${kernel}/lib "$out/lib"
      '';
  inputs = pkgs.runCommand "koi-inputs" { } ''
    mkdir -p "$out"
    ln -s ${bootKernel} "$out/kernel"
    cp ${./stage1.sh} "$out/stage1.sh"
    cp ${./first-boot.sh} "$out/first-boot.sh"
    cp ${ready}/bin/koi-ready "$out/ready"
    cp ${ready}/bin/koi-prefetch "$out/prefetch"
    cp ${pkgs.pkgsStatic.busybox}/bin/busybox "$out/busybox"
    cp ${./prepare-image.sh} "$out/prepare-image.sh"
    cp ${./network.sh} "$out/network.sh"
    ln -s ${guestTools} "$out/guest-tools"
  '';
  builder = pkgs.writeShellApplication {
    name = "koi-build-image";
    runtimeInputs = with pkgs; [
      python3
      fakeroot
      e2fsprogs
      gptfdisk
      coreutils
      gitMinimal
    ];
    text = ''
      if [[ $# != 1 ]]; then
        echo "usage: koi-build-image OUTPUT_DIRECTORY" >&2
        exit 2
      fi
      exec fakeroot -- python3 ${./build.py} ${inputs} "$1"
    '';
  };
  image = pkgs.runCommand "koi-nix-runit-image" { nativeBuildInputs = [ builder ]; } ''
    koi-build-image "$out"
    chmod 755 "$out"
  '';
in
{
  inherit
    kernel
    inputs
    builder
    guestTools
    image
    ;
}
