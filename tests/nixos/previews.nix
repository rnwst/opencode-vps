{ pkgs, pkgsUnstable }:
let
  # Never import deployment settings: those are also embedded in the wrapper.
  settings = {
    publicHostName = "opencode.example.com";
    opencodePort = 4096;
    workspacesRoot = "/srv/opencode/workspaces";
    workspacesTmpRoot = "/srv/opencode/workspace-tmp";
    githubBridge = {
      enable = false;
      canonicalRoot = "/var/lib/github-bridge/canonical";
      stateRoot = "/var/lib/github-bridge";
    };
    previews = {
      domain = "example.com";
      # Shared pool for the concurrent Chromium sessions, not a per-runtime cap.
      memoryMax = 1073741824;
      tasksMax = 256;
      # Nondefault value exercises settings -> manager -> sandbox supervisor.
      maxExecs = 2;
    };
    # Immutable, explicitly fake credentials, not operator secret paths.
    secrets = {
      serverPassword = pkgs.writeText "preview-test-password" "fakepassword";
      githubToken = pkgs.writeText "preview-test-github-token" "fakegh";
    };
  };
  testLocalPackages = import ../../pkgs { inherit pkgs pkgsUnstable settings; };
  preview = testLocalPackages.opencode-preview;
  python = pkgs.python3.withPackages (p: [ p.aiohttp ]);
  fixture = "${python}/bin/python3 ${./preview-fixture.py}";
  task = ".tasks/task-preview-long-workspace-name-0123456789abcdefghijklmnopqrstuvwxyz";
in
pkgs.testers.nixosTest {
  name = "opencode-previews";
  nodes.machine = { lib, ... }: {
    imports = [ ../../modules/opencode-previews.nix ];
    _module.args = {
      inherit settings;
      localPackages = testLocalPackages;
    };
    virtualisation.memorySize = 4096;
    virtualisation.cores = 2;
    # Exercise pool-local OOM without a panic, but still fail on VM-wide OOM.
    boot.kernel.sysctl."vm.panic_on_oom" = lib.mkForce 1;
    networking.firewall.enable = false;
    users.groups.agent-workspaces = { };
    users.users.rnwst-bot = {
      isSystemUser = true;
      group = "agent-workspaces";
      home = "/home/rnwst-bot";
      createHome = true;
    };
    # The production service deliberately uses only the system/profile PATH.
    environment.systemPackages = with pkgs; [
      curl
      python3
    ];
    systemd.services.opencode-workspace-temp-init = {
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      script = ''
        install -d -m 0711 ${settings.workspacesRoot} ${settings.workspacesTmpRoot}
        for root in ${settings.workspacesRoot} ${settings.workspacesTmpRoot}; do
          install -d -m 0711 "$root/.tasks"
          for workspace in alpha beta ${task}; do
            install -d -m 0700 -o rnwst-bot -g agent-workspaces "$root/$workspace"
          done
        done
      '';
    };
    # Keep the real module's delegation, subgroup, hardening and lifecycle.
    systemd.services.opencode-previews.serviceConfig.LoadCredential = lib.mkForce [
      "github-token:${settings.secrets.githubToken}"
      "server-password:${settings.secrets.serverPassword}"
    ];
    # This exact name exercises the dependencies added by the preview module.
    systemd.services.opencode = {
      wantedBy = [ "multi-user.target" ];
      serviceConfig.ExecStart = "${fixture} serve";
    };
  };
  testScript = ''
    machine.start()
    machine.wait_for_unit("opencode-previews.service")
    machine.wait_for_unit("opencode.service")
    machine.wait_for_open_port(4080)
    machine.wait_for_open_port(4096)
    try:
        machine.succeed(
            "${fixture} test ${preview}/bin/opencode-session-exec ${preview.configFile} ${task}",
            timeout=600,
        )
    finally:
        print(machine.succeed("journalctl -u opencode-previews -u opencode --no-pager"))
        print(machine.succeed("systemd-cgls --all --no-pager"))
  '';
}
