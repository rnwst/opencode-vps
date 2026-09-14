{
  pkgs,
  settings,
  sandboxExec,
  playwrightMcp,
}:
let
  cfg = import ../../config/previews.nix {
    inherit settings;
    inherit (pkgs) lib;
  };
  python = pkgs.python3.withPackages (p: [ p.aiohttp ]);
  source = pkgs.runCommand "opencode-preview-source" { } ''
    mkdir -p "$out"
    cp ${./gateway.py} "$out/gateway.py"
    cp ${./manager.py} "$out/manager.py"
    cp ${./client.py} "$out/client.py"
    cp ${./supervisor.py} "$out/supervisor.py"
    cp ${./mcp_transport.py} "$out/mcp_transport.py"
    cp ${./mcp_frontend.py} "$out/mcp_frontend.py"
    cp ${./launch.py} "$out/launch.py"
  '';
  configFile = pkgs.writeText "opencode-previews.json" (
    builtins.toJSON {
      public_host = settings.publicHostName or "opencode.example.com";
      preview_domain = cfg.domain;
      opencode_port = settings.opencodePort;
      listen_port = cfg.gatewayPort;
      runtime_root = cfg.runtimeRoot;
      workspaces_root = settings.workspacesRoot;
      workspaces_tmp_root = settings.workspacesTmpRoot;
      sandbox_exec = "${sandboxExec}/bin/opencode-sandbox-exec";
      supervisor = "${source}/supervisor.py";
      launch = "${source}/launch.py";
      python = "${pkgs.python3}/bin/python3";
      shell = "${pkgs.bash}/bin/bash";
      playwright_mcp = "${playwrightMcp}/bin/opencode-playwright-mcp";
      memory_max = cfg.memoryMax;
      tasks_max = cfg.tasksMax;
      cpu_quota = cfg.cpuQuota;
      max_runtimes = cfg.maxRuntimes;
      max_ports = cfg.maxPorts;
      max_connections = cfg.maxConnections;
      max_lifetime_seconds = cfg.maxLifetimeSeconds;
      idle_timeout_seconds = cfg.idleTimeoutSeconds;
    }
  );
  client = pkgs.writeShellApplication {
    name = "opencode-session-exec";
    text = ''exec ${python}/bin/python3 ${source}/client.py ${configFile} "$@"'';
  };
  server = pkgs.writeShellApplication {
    name = "opencode-preview-server";
    text = "exec ${python}/bin/python3 ${source}/gateway.py ${configFile}";
  };
  catalog = pkgs.runCommand "opencode-playwright-tools.json" { } ''
    export HOME="$TMPDIR"
    ${python}/bin/python3 ${source}/mcp_frontend.py catalog \
      ${pkgs.playwright-mcp}/bin/playwright-mcp > "$out"
  '';
  mcp = pkgs.writeShellApplication {
    name = "opencode-session-mcp";
    text = "exec ${python}/bin/python3 ${source}/mcp_frontend.py serve ${configFile} ${catalog}";
  };
in
pkgs.symlinkJoin {
  name = "opencode-preview";
  paths = [
    client
    server
    mcp
  ];
  passthru = { inherit configFile source python; };
}
