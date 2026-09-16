{
  lib,
  localPackages,
  pkgs,
  settings,
  ...
}:
let
  botHome = "/home/${settings.accounts.bot.name}";
  previewCfg = import ../config/previews.nix { inherit lib settings; };
  managedConfig = pkgs.writeText "opencode-managed.json" (
    builtins.toJSON {
      "$schema" = "https://opencode.ai/config.json";
      autoupdate = false;
      enabled_providers = [ "openai" ];
      model = "${settings.defaultModel.providerID}/${settings.defaultModel.modelID}";
      mcp = lib.optionalAttrs previewCfg.enable {
        playwright = {
          type = "local";
          command = [ "${localPackages.opencode-preview}/bin/opencode-session-mcp" ];
          enabled = true;
          timeout = 130000;
        };
      };
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
          "/tmp" = "allow";
          "/tmp/*" = "allow";
          "/tmp/**" = "allow";
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
      shell =
        if previewCfg.enable then
          "${localPackages.opencode-preview}/bin/opencode-session-exec"
        else
          "${localPackages.sandbox-exec}/bin/opencode-sandbox-exec";
      snapshot = false;
    }
  );

  agentsFile = pkgs.writeText "opencode-AGENTS.md" (
    builtins.replaceStrings [ "@GITHUB_REVIEWER@" ] [ "@${settings.githubReviewer}" ] (
      builtins.readFile ../config/AGENTS.md
    )
  );
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
      after = [
        "network-online.target"
        "opencode-workspace-temp-init.service"
      ];
      requires = [ "opencode-workspace-temp-init.service" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];
      restartTriggers = [
        agentsFile
        managedConfig
        localPackages.opencode-plugin
      ];

      environment = {
        HOME = botHome;
        OPENCODE_DISABLE_PROJECT_CONFIG = "1";
        OPENCODE_DISABLE_LSP_DOWNLOAD = "true";
        OPENCODE_DISABLE_MODELS_FETCH = "false";
        PATH = lib.mkForce "/etc/profiles/per-user/${settings.accounts.bot.name}/bin:/run/current-system/sw/bin";
        XDG_CACHE_HOME = "${botHome}/.cache";
        XDG_CONFIG_HOME = "${botHome}/.config";
        XDG_DATA_HOME = "${botHome}/.local/share";
      };

      serviceConfig = {
        User = settings.accounts.bot.name;
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
          botHome
          "/var/lib/ci-runner/jobs"
          settings.workspacesRoot
          settings.workspacesTmpRoot
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
      ingress = {
        ${settings.publicHostName} = "http://127.0.0.1:${
          toString (if previewCfg.enable then previewCfg.gatewayPort else settings.opencodePort)
        }";
      }
      // lib.optionalAttrs previewCfg.enable {
        "*.${previewCfg.domain}" = "http://127.0.0.1:${toString previewCfg.gatewayPort}";
      };
    };
  };
}
