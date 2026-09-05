{
  lib,
  localPackages,
  pkgs,
  settings,
  ...
}:
let
  managedConfig = pkgs.writeText "opencode-managed.json" (
    builtins.toJSON {
      "$schema" = "https://opencode.ai/config.json";
      autoupdate = false;
      enabled_providers = [ "openai" ];
      formatter.jlfmt = {
        command = [
          "jlfmt"
          "--inplace"
          "$FILE"
        ];
        extensions = [ ".jl" ];
      };
      instructions = [ "/etc/opencode/AGENTS.md" ];
      lsp = {
        jetls = {
          command = [
            "jetls"
            "serve"
          ];
          extensions = [ ".jl" ];
        };
        julials.disabled = true;
        lemminx = {
          command = [ "lemminx" ];
          extensions = [
            ".rels"
            ".xml"
          ];
        };
      };
      permission = {
        "*" = "allow";
        external_directory = {
          "*" = "deny";
          "/etc/opencode/AGENTS.md" = "allow";
          "/nix/store/**" = "allow";
        };
      };
      plugin = [ "file:///etc/opencode/plugins/managed-host.js" ];
      server = {
        cors = [ "https://${settings.publicHostName}" ];
        hostname = "127.0.0.1";
        mdns = false;
        port = settings.opencodePort;
      };
      share = "disabled";
      shell = "${localPackages.sandbox-exec}/bin/opencode-sandbox-exec";
      snapshot = false;
    }
  );

  agentsFile = ../config/AGENTS.md;
  tunnelEnabled = settings.cloudflareTunnelId != null;
in
{
  environment = {
    etc = {
      "opencode/AGENTS.md".source = agentsFile;
      "opencode/opencode.json".source = managedConfig;
      "opencode/plugins/managed-host.js".source = localPackages.opencode-plugin;
    };
    systemPackages = [
      localPackages.opencode-git
      localPackages.opencode
      localPackages.opencode-server
      localPackages.sandbox-exec
    ];
  };

  systemd.services = {
    opencode = {
      description = "OpenCode autonomous development server";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];
      restartTriggers = [
        agentsFile
        managedConfig
        localPackages.opencode-plugin
      ];

      environment = {
        HOME = "/home/rnwst-bot";
        OPENCODE_DISABLE_PROJECT_CONFIG = "1";
        OPENCODE_DISABLE_LSP_DOWNLOAD = "true";
        PATH = lib.mkForce "/etc/profiles/per-user/rnwst-bot/bin:/run/current-system/sw/bin";
        XDG_CACHE_HOME = "/home/rnwst-bot/.cache";
        XDG_CONFIG_HOME = "/home/rnwst-bot/.config";
        XDG_DATA_HOME = "/home/rnwst-bot/.local/share";
      };

      serviceConfig = {
        User = "rnwst-bot";
        Group = "agent-workspaces";
        WorkingDirectory = settings.workspacesRoot;
        ExecStart = "${localPackages.opencode-server}/bin/opencode-server";
        LoadCredential = [
          "github-token:${settings.secrets.githubToken}"
          "server-password:${settings.secrets.serverPassword}"
        ];
        Restart = "on-failure";
        RestartSec = "5s";
        UMask = "0027";

        LockPersonality = true;
        PrivateDevices = true;
        PrivateTmp = true;
        # ProtectKernelLogs/Tunables lock proc mounts required by bubblewrap.
        ProtectClock = true;
        ProtectControlGroups = true;
        ProtectKernelModules = true;
        ProtectSystem = "strict";
        ReadWritePaths = [
          "/home/rnwst-bot"
          "/var/lib/ci-runner/jobs"
          settings.workspacesRoot
        ]
        ++ lib.optionals settings.githubBridge.enable [
          "${settings.githubBridge.stateRoot}/inbox"
          "${settings.githubBridge.stateRoot}/responses"
        ];
        SupplementaryGroups = lib.optionals settings.githubBridge.enable [ "github-bridge-register" ];
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_NETLINK"
          "AF_UNIX"
        ];
        RestrictRealtime = true;
      };
    };
  };

  services.cloudflared = lib.mkIf tunnelEnabled {
    enable = true;
    tunnels.${settings.cloudflareTunnelId} = {
      credentialsFile = settings.secrets.cloudflareCredentials;
      default = "http_status:404";
      ingress.${settings.publicHostName} = "http://127.0.0.1:${toString settings.opencodePort}";
    };
  };
}
