{
  pkgs,
  settings,
  workspaceManager,
}:

pkgs.writeShellApplication {
  name = "github-bridge";
  runtimeInputs = with pkgs; [
    coreutils
    python3
    util-linux
  ];
  text = ''
    export GITHUB_BRIDGE_STATE_ROOT=${settings.githubBridge.stateRoot}
    export GITHUB_BRIDGE_WORKSPACES_ROOT=${settings.workspacesRoot}
    export GITHUB_BRIDGE_WORKSPACE_MANAGER=${workspaceManager}/bin/opencode-workspace
    export GITHUB_BRIDGE_AGENT=${settings.githubBridge.agent}
    export GITHUB_BRIDGE_PROVIDER=${settings.githubBridge.model.providerID}
    export GITHUB_BRIDGE_MODEL=${settings.githubBridge.model.modelID}
    export GITHUB_BRIDGE_DRY_RUN=${if settings.githubBridge.dryRun then "1" else "0"}
    export GITHUB_BRIDGE_MAX_TASKS=${toString settings.githubBridge.maxConcurrentTasks}
    export GITHUB_BRIDGE_RETENTION_DAYS=${toString settings.githubBridge.retentionDays}
    export GITHUB_BRIDGE_OPENCODE_URL=http://127.0.0.1:${toString settings.opencodePort}
    export GITHUB_BRIDGE_GITHUB_TOKEN_FILE=${settings.secrets.githubToken}
    export GITHUB_BRIDGE_SERVER_PASSWORD_FILE=${settings.secrets.serverPassword}
    export GITHUB_BRIDGE_CONTROLLER_ID_FILE=${settings.secrets.githubControllerId}
    exec ${pkgs.python3}/bin/python3 ${./bridge.py} "$@"
  '';
}
