{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.koi;
  json = pkgs.formats.json { };
  hostConfig =
    if cfg.settings == null then
      cfg.configFile
    else
      json.generate "koi-host.json" (cfg.settings // { networkMode = "local"; });
in
{
  options.services.koi = {
    enable = lib.mkEnableOption "Koi VM hosting";
    package = lib.mkOption {
      type = lib.types.package;
      default = import ./package.nix { inherit pkgs; };
    };
    configFile = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/koi/host.json";
      description = "Mutable host configuration supplied by deployment.";
    };
    settings = lib.mkOption {
      type = lib.types.nullOr json.type;
      default = null;
      description = "Host configuration: resources, images and ordinary networking.";
    };
    admin = {
      enable = lib.mkEnableOption "sudo-gated Koi administration";
      users = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
      };

    };
  };
  config = lib.mkMerge [
    (lib.mkIf cfg.admin.enable {
      assertions = [
        {
          assertion = cfg.admin.users != [ ];
          message = "Koi administration needs authorized users.";
        }
      ];
      environment.systemPackages = [ cfg.package ];
      security.sudo.extraRules = [
        {
          users = cfg.admin.users;
          commands = [
            {
              command = "${cfg.package}/bin/koi-admin *";
              options = [ "PASSWD" ];
            }
          ];
        }
      ];
    })
    (lib.mkIf cfg.enable {
      assertions = [
        {
          assertion = config.virtualisation.incus.enable;
          message = "Koi requires host-owned Incus; enable virtualisation.incus separately and retain it when removing Koi.";
        }
      ];
      environment.etc."koi/host.json".source = hostConfig;
      environment.systemPackages = [
        cfg.package
        pkgs.cdrkit
      ];
      networking.nftables.enable = true;
      networking.nftables.flushRuleset = false;
      networking.firewall.filterForward = true;
      networking.firewall.interfaces.koi-local = {
        allowedUDPPorts = [
          53
          67
        ];
        allowedTCPPorts = [ 53 ];
      };
      networking.firewall.extraForwardRules = ''
        iifname "koi-local" accept
        oifname "koi-local" ct state established,related accept
      '';
      environment.etc."hosts".mode = "0644";
      systemd.tmpfiles.rules = [
        "d /var/lib/koi 0755 root root -"
      ];
      systemd.services.koi = {
        wantedBy = [ "multi-user.target" ];
        requires = [ "incus.service" ];
        after = [ "incus.service" ];
        unitConfig.ConditionPathExists = toString hostConfig;
        restartTriggers = [
          hostConfig
          config.environment.etc."hosts".source
        ];
        serviceConfig = {
          Type = "simple";
          RuntimeDirectory = "koi";
          RuntimeDirectoryMode = "0700";
          StateDirectory = "koi";
          StateDirectoryMode = "0755";
          ExecStartPre = "${cfg.package}/bin/koi-host reconcile";
          ExecStart = "${cfg.package}/bin/koi-daemon";
          ExecStopPost = "${cfg.package}/bin/koi-host withdraw";
        };
      };
    })
  ];
}
