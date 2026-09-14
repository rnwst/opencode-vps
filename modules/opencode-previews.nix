{
  lib,
  localPackages,
  settings,
  ...
}:
let
  cfg = import ../config/previews.nix { inherit lib settings; };
in
{
  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion =
          cfg.gatewayPort > 0 && cfg.gatewayPort < 65536 && cfg.gatewayPort != settings.opencodePort;
        message = "The preview gateway needs a distinct valid local port.";
      }
      {
        assertion = builtins.match "[a-z0-9][a-z0-9.-]*\\.[a-z0-9.-]+" cfg.domain != null;
        message = "previews.domain must be a lowercase DNS domain.";
      }
      {
        assertion = cfg.runtimeRoot == "/run/opencode-previews";
        message = "Preview control paths must use the protected systemd runtime directory.";
      }
    ];
    environment.systemPackages = [ localPackages.opencode-preview ];
    systemd.services.opencode-previews = {
      description = "Isolated OpenCode session runtimes and preview gateway";
      wantedBy = [ "multi-user.target" ];
      after = [
        "network-online.target"
        "opencode-workspace-temp-init.service"
      ];
      requires = [ "opencode-workspace-temp-init.service" ];
      wants = [ "network-online.target" ];
      environment = {
        HOME = "/home/rnwst-bot";
        PATH = lib.mkForce "/etc/profiles/per-user/rnwst-bot/bin:/run/current-system/sw/bin";
      };
      serviceConfig = {
        User = "rnwst-bot";
        Group = "agent-workspaces";
        WorkingDirectory = settings.workspacesRoot;
        ExecStart = "${localPackages.opencode-preview}/bin/opencode-preview-server";
        LoadCredential = [
          "github-token:${settings.secrets.githubToken}"
          "server-password:${settings.secrets.serverPassword}"
        ];
        RuntimeDirectory = "opencode-previews";
        RuntimeDirectoryMode = "0700";
        UMask = "0077";
        Restart = "on-failure";
        RestartSec = "3s";
        TimeoutStopSec = "30s";
        KillMode = "control-group";
        # Pool OOM kills a selected runtime, not the manager or its siblings.
        OOMPolicy = "continue";
        Delegate = [
          "cpu"
          "memory"
          "pids"
        ];
        DelegateSubgroup = "manager";
        LimitNOFILE = 8192;
        LimitCORE = 0;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectSystem = "strict";
        ReadWritePaths = [
          settings.workspacesRoot
          settings.workspacesTmpRoot
          cfg.runtimeRoot
        ];
        LockPersonality = true;
        ProtectClock = true;
        ProtectKernelModules = true;
        RestrictRealtime = true;
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_NETLINK"
          "AF_UNIX"
        ];
        # The manager owns delegated cgroups; SRT makes them read-only in workloads.
        ProtectControlGroups = false;
      };
    };
    systemd.services.opencode = {
      after = [ "opencode-previews.service" ];
      wants = [ "opencode-previews.service" ];
    };
  };
}
