{ pkgs }:
pkgs.stdenv.mkDerivation {
  pname = "koi-q35-trial-kernel";
  inherit (pkgs.linux) version src;

  baseline = pkgs.fetchurl {
    url = "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/20260930-a738f18a8db0-0/x86_64/bzImage-6.18.51";
    sha256 = "bf8f6252032d5644900e7216509c86d16b5d7264264034bcfe89cd8aceb11481";
  };
  nativeBuildInputs = with pkgs; [
    bc
    bison
    flex
    lz4
    perl
    python3
    pkg-config
  ];
  buildInputs = with pkgs; [
    elfutils
    openssl
  ];
  hardeningDisable = [ "all" ];
  enableParallelBuilding = true;
  dontStrip = true;
  KBUILD_BUILD_TIMESTAMP = "1970-01-01";
  KBUILD_BUILD_USER = "koi";
  KBUILD_BUILD_HOST = "nix";

  postPatch = "patchShebangs scripts";
  configurePhase = ''
    runHook preConfigure
    scripts/extract-ikconfig "$baseline" > .config
    scripts/config --set-str LOCALVERSION -koi-q35 --disable LOCALVERSION_AUTO \
      --enable SCSI --enable BLK_DEV_SD --enable SCSI_VIRTIO \
      --enable BLK_DEV_WRITE_MOUNTED \
      --enable EFI_PARTITION \
      --enable NET_9P --enable NET_9P_VIRTIO --enable 9P_FS \
      --enable FUSE_FS --enable VIRTIO_FS --enable ACPI_BUTTON \
      --disable SECURITY_APPARMOR \
      --disable KERNEL_GZIP --enable KERNEL_LZ4
    make olddefconfig
    for option in SCSI_VIRTIO BLK_DEV_SD VIRTIO_PCI VIRTIO_NET EXT4_FS \
      NET_9P_VIRTIO 9P_FS VIRTIO_FS VIRTIO_VSOCKETS ACPI_BUTTON \
      BLK_DEV_WRITE_MOUNTED EFI_PARTITION; do
      grep -qx "CONFIG_$option=y" .config
    done
    grep -qx '# CONFIG_SECURITY_APPARMOR is not set' .config
    grep -qx 'CONFIG_KERNEL_LZ4=y' .config
    runHook postConfigure
  '';
  buildPhase = ''
    runHook preBuild
    make -j"$NIX_BUILD_CORES" bzImage modules
    runHook postBuild
  '';
  installPhase = ''
    runHook preInstall
    mkdir -p "$out"
    cp arch/x86/boot/bzImage .config System.map "$out/"
    make INSTALL_MOD_PATH="$out" modules_install
    rm -f "$out"/lib/modules/*/build "$out"/lib/modules/*/source
    runHook postInstall
  '';
}
