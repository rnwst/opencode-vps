{
  lib,
  localPackages,
  pkgs,
  settings,
  ...
}:
let
  cfg = settings.githubBridge;
  inherit (cfg) stateRoot;
in
{
  config = lib.mkIf cfg.enable {
    users = {
      groups = {
        github-bridge = { };
        github-bridge-register = { };
      };
      users = {
        github-bridge = {
          isSystemUser = true;
          group = "github-bridge";
          extraGroups = [ "github-bridge-register" ];
          home = stateRoot;
          shell = "${pkgs.shadow}/bin/nologin";
        };
      };
    };

    security.sudo.extraRules = [
      {
        users = [ "github-bridge" ];
        runAs = "root";
        commands = [
          {
            command = "${localPackages.opencode-workspace}/bin/opencode-workspace";
            options = [ "NOPASSWD" ];
          }
        ];
      }
    ];

    environment.systemPackages = [ localPackages.github-bridge ];

    systemd = {
      tmpfiles.rules = [
        "d ${stateRoot} 0710 github-bridge github-bridge-register -"
        "d ${stateRoot}/inbox 2770 github-bridge github-bridge-register -"
        "d ${stateRoot}/responses 2770 github-bridge github-bridge-register -"
      ];

      services.github-bridge = {
        description = "GitHub event bridge for OpenCode";
        after = [
          "network-online.target"
          "opencode.service"
        ];
        wants = [ "network-online.target" ];
        serviceConfig = {
          Type = "oneshot";
          User = "github-bridge";
          Group = "github-bridge";
          SupplementaryGroups = [ "github-bridge-register" ];
          ExecStart = "${localPackages.github-bridge}/bin/github-bridge run";
          LoadCredential = [
            "github-token:${settings.secrets.githubToken}"
            "server-password:${settings.secrets.serverPassword}"
            "controller-id:${settings.secrets.githubControllerId}"
          ];
          UMask = "0027";

          LockPersonality = true;
          PrivateDevices = true;
          PrivateTmp = true;
          ProtectClock = true;
          ProtectControlGroups = true;
          ProtectHome = "read-only";
          ProtectKernelLogs = true;
          ProtectKernelModules = true;
          ProtectKernelTunables = true;
          ProtectSystem = "strict";
          ReadWritePaths = [
            stateRoot
            cfg.canonicalRoot
            settings.workspacesRoot
            settings.workspacesTmpRoot
            "/run/opencode-workspace"
          ];
          RestrictAddressFamilies = [
            "AF_INET"
            "AF_INET6"
            "AF_UNIX"
          ];
          RestrictRealtime = true;
        };
      };

      timers.github-bridge = {
        description = "Poll GitHub for OpenCode work";
        wantedBy = [ "timers.target" ];
        timerConfig = {
          OnBootSec = "1m";
          OnUnitActiveSec = "1m";
          Unit = "github-bridge.service";
        };
      };

      paths.github-bridge-inbox = {
        description = "Process managed OpenCode GitHub requests";
        wantedBy = [ "multi-user.target" ];
        pathConfig = {
          PathChanged = "${stateRoot}/inbox";
          Unit = "github-bridge.service";
        };
      };
    };
  };
}
