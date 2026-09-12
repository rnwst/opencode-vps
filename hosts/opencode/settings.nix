{
  defaultModel = {
    providerID = "openai";
    modelID = "gpt-6-astra-fast";
  };
  # This file contains the public, installation-specific values a new operator
  # should replace when reusing the repository.
  hostName = "opencode";
  publicHostName = "opencode.example.com";

  # Set this to the stable identifier observed during the target preflight.
  # Leaving the sentinel in place makes disko fail rather than guessing a disk.
  diskDevice = "/dev/disk/by-id/REPLACE-ME";

  # Create the tunnel before deployment, then place its UUID here. Cloudflared
  # remains disabled while this is null.
  cloudflareTunnelId = null;

  operatorKeys = [ ];

  opencodePort = 4096;
  workspacesRoot = "/srv/opencode/workspaces";
  workspacesTmpRoot = "/srv/opencode/workspace-tmp";

  githubBridge = {
    enable = true;
    dryRun = false;
    agent = "build";
    model = {
      providerID = "openai";
      modelID = "gpt-6-astra";
    };
    maxConcurrentTasks = 4;
    minimumFreePercent = 15;
    retentionDays = 30;
    canonicalRoot = "/var/lib/opencode-task-bases";
    stateRoot = "/var/lib/opencode-github-bridge";
  };

  secrets = {
    directory = "/var/lib/opencode-secrets";
    cloudflareCredentials = "/var/lib/opencode-secrets/cloudflared.json";
    serverPassword = "/var/lib/opencode-secrets/server-password";
    githubToken = "/var/lib/opencode-secrets/github-token";
    githubControllerId = "/var/lib/opencode-secrets/github-controller-id";
  };
}
