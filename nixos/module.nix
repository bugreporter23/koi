{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.koi;
  settings = pkgs.writeText "koi-settings.json" (
    builtins.toJSON {
      state = cfg.stateDir;
      dataset = if cfg.dataset == null then "koi-vm/vms" else cfg.dataset;
      poolSize = if cfg.dataset == null then cfg.poolSize else null;
      inherit (cfg) subnet operator;
    }
  );
  zfs = config.boot.zfs.package;
  helper = pkgs.writeShellScript "koi-root" ''
    export PATH=${zfs}/bin:/run/current-system/sw/bin
    exec ${cfg.package}/bin/koi-host --managed "$@"
  '';
  admin = pkgs.writeShellScript "koi-host" ''
    export PATH=${zfs}/bin:/run/current-system/sw/bin
    exec ${cfg.package}/bin/koi-host "$@"
  '';
in
{
  options.services.koi = {
    enable = lib.mkEnableOption "koi persistent per-project VMs";
    package = lib.mkOption {
      type = lib.types.package;
      description = "Host backend (flake output `koi`); its matching user tools are installed automatically.";
    };
    operator = lib.mkOption {
      type = lib.types.str;
      description = "Local user allowed to run VM operations without a password.";
    };
    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/koi-vm";
      description = "Root-private state: records, keys, images and the default pool file.";
    };
    dataset = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "tank/koi";
      description = ''
        New namespace below an existing ZFS pool. Null creates an owned pool
        `koi-vm` in a sparse file at STATE/pool.img.
      '';
    };
    poolSize = lib.mkOption {
      type = lib.types.str;
      default = "32GiB";
      description = "Capacity of the default sparse pool file, on initial creation.";
    };
    subnet = lib.mkOption {
      type = lib.types.str;
      default = "10.201.0.0/24";
      description = "Private IPv4 /24 for guests; must not overlap a host route.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = pkgs.stdenv.hostPlatform.system == "x86_64-linux";
        message = "koi requires x86_64-linux.";
      }
      {
        assertion = config.boot.zfs.enabled;
        message = "koi requires ZFS: add \"zfs\" to boot.supportedFilesystems.";
      }
      {
        assertion = config.users.mutableUsers;
        message = "koi creates per-VM accounts at runtime; set users.mutableUsers = true.";
      }
    ];
    environment.systemPackages = [
      cfg.package
      cfg.package.userTools
    ];
    environment.etc."koi/client-id".text = cfg.package.clientId + "\n";
    environment.etc."koi/settings.json".source = settings;
    environment.etc."koi/bin/koi-root".source = helper;
    environment.etc."koi/bin/koi-host".source = admin;
    environment.etc."koi/bin/koi-login".source = "${cfg.package}/bin/koi-login";
    security.sudo.extraRules = [
      {
        users = [ cfg.operator ];
        runAs = "root";
        commands = [
          {
            command = "/etc/koi/bin/koi-root";
            options = [ "NOPASSWD" ];
          }
        ];
      }
    ];
    systemd.services.koi = {
      description = "Koi owned guest network";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];
      path = [ zfs ];
      unitConfig = {
        ConditionPathExists = "/etc/koi/installation.json";
        RequiresMountsFor = [ cfg.stateDir ];
      };
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${cfg.package}/bin/koi-host network-up";
        ExecStopPost = "${cfg.package}/bin/koi-host network-stop";
        TimeoutStopSec = 300;
      };
      restartIfChanged = false;
    };
  };
}
