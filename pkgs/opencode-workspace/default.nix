{
  githubApiUrl ? "https://api.github.com",
  pkgs,
  sandboxExec,
  settings,
  testMode ? false,
}:

pkgs.writeShellApplication {
  name = "opencode-workspace";
  runtimeInputs = with pkgs; [
    acl
    btrfs-progs
    coreutils
    curl
    findutils
    git
    gnugrep
    jq
    util-linux
  ];
  text = ''
    export OPENCODE_WORKSPACES_ROOT=${settings.workspacesRoot}
    export OPENCODE_WORKSPACES_TMP_ROOT=${settings.workspacesTmpRoot}
    export OPENCODE_CANONICAL_ROOT=${settings.githubBridge.canonicalRoot}
    export OPENCODE_GITHUB_TOKEN_FILE=${settings.secrets.githubToken}
    export OPENCODE_MINIMUM_FREE_PERCENT=${toString settings.githubBridge.minimumFreePercent}
    export OPENCODE_SANDBOX_EXEC=${sandboxExec}/bin/opencode-sandbox-exec
    export OPENCODE_GITHUB_API_URL=${githubApiUrl}
    export OPENCODE_WORKSPACE_TEST_MODE=${if testMode then "1" else "0"}
    ${pkgs.lib.removePrefix "#!/usr/bin/env bash\n" (builtins.readFile ./opencode-workspace.sh)}
  '';
}
