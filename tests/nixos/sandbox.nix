{ pkgs, pkgsUnstable }:
let
  settings = (import ../../hosts/opencode/settings.nix) // {
    accounts = {
      bot = {
        name = "test-bot";
        git = {
          name = "Test Bot";
          email = "test-bot@example.com";
        };
      };
      admin = {
        name = "test-admin";
        git = {
          name = "Test Admin";
          email = "test-admin@example.com";
        };
      };
    };
    githubReviewer = "test-reviewer";
  };
  localPackages = import ../../pkgs { inherit pkgs pkgsUnstable settings; };
  botHome = "/home/${settings.accounts.bot.name}";
  # A real task-length path must not overflow Linux's Unix socket pathname limit.
  name = ".tasks/task-security-long-workspace-name-0123456789abcdef";
  workspace = "${settings.workspacesRoot}/${name}";
  temporary = "${settings.workspacesTmpRoot}/${name}";
  workload = ./sandbox-workload.py;
  python = "${pkgs.python3}/bin/python3 ${workload}";
  wrapper = "${localPackages.sandbox-exec}/bin/opencode-sandbox-exec";
in
pkgs.testers.nixosTest {
  name = "sandbox-security";
  nodes.machine = {
    virtualisation.memorySize = 2048;
    networking.firewall.enable = false;
    networking.hosts = {
      "93.184.216.34" = [
        "github.com"
        "api.github.com"
      ];
      "10.55.0.1" = [ "private.github.com" ];
    };
    users.groups.agent-workspaces = { };
    users.users.${settings.accounts.bot.name} = {
      isSystemUser = true;
      group = "agent-workspaces";
      home = botHome;
      createHome = true;
    };
    environment.systemPackages = with pkgs; [
      acl
      curl
      python3
    ];
    systemd.services.sandbox-fixture = {
      wantedBy = [ "multi-user.target" ];
      path = with pkgs; [
        acl
        iproute2
        openssl
      ];
      preStart = ''
        install -d -m 0711 ${settings.workspacesRoot} ${settings.workspacesTmpRoot}
        install -d -m 0700 -o ${settings.accounts.bot.name} -g agent-workspaces ${workspace} ${temporary}
        setfacl -m u:${settings.accounts.bot.name}:rwx,d:u:${settings.accounts.bot.name}:rwx ${workspace} ${temporary}
        install -d -m 0755 /run/sandbox-fixture
        install -d -m 0700 /run/sandbox-fixture/private
        openssl rand -hex 24 > /run/sandbox-fixture/private/token
        openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=VM-test-CA \
          -keyout /run/sandbox-fixture/private/ca.key -out /run/sandbox-fixture/ca.crt
        openssl req -newkey rsa:2048 -nodes -subj /CN=github.com \
          -addext 'subjectAltName=DNS:github.com,DNS:api.github.com,DNS:private.github.com' \
          -keyout /run/sandbox-fixture/private/leaf.key -out /run/sandbox-fixture/private/leaf.csr
        openssl x509 -req -days 2 -copy_extensions copy \
          -in /run/sandbox-fixture/private/leaf.csr -CA /run/sandbox-fixture/ca.crt \
          -CAkey /run/sandbox-fixture/private/ca.key -CAcreateserial -out /run/sandbox-fixture/leaf.crt
        ip link add public-fixture type dummy
        ip address add 93.184.216.34/32 dev public-fixture
        ip address add 10.55.0.1/32 dev public-fixture
        ip address add 169.254.169.254/32 dev public-fixture
        ip link set public-fixture up
      '';
      serviceConfig.ExecStart = "${python} serve";
    };
    systemd.services.sandbox-probe = {
      requires = [ "sandbox-fixture.service" ];
      after = [ "sandbox-fixture.service" ];
      path = with pkgs; [
        curl
        python3
      ];
      environment = {
        HOME = botHome;
        NODE_EXTRA_CA_CERTS = "/run/sandbox-fixture/ca.crt";
      };
      # Keep the sandbox-relevant hardening in modules/opencode.nix, without
      # installing OpenCode or giving the test a less restricted service.
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        TimeoutStartSec = 180;
        User = settings.accounts.bot.name;
        Group = "agent-workspaces";
        WorkingDirectory = workspace;
        LoadCredential = [ "github-token:/run/sandbox-fixture/private/token" ];
        UMask = "0027";
        LockPersonality = true;
        PrivateDevices = true;
        PrivateTmp = true;
        ProtectClock = true;
        ProtectControlGroups = true;
        ProtectKernelModules = true;
        ProtectSystem = "strict";
        ReadWritePaths = [
          botHome
          settings.workspacesRoot
          settings.workspacesTmpRoot
        ];
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_NETLINK"
          "AF_UNIX"
        ];
        RestrictRealtime = true;
      };
      script = ''
        ${python} outside
        OPENCODE_SESSION_ID=ses_one ${wrapper} -c '${python} inside'
        OPENCODE_SESSION_ID=ses_one ${wrapper} -c 'test "$(cat /tmp/persist)" = session-one'
        OPENCODE_SESSION_ID=ses_two ${wrapper} -c \
          'test ! -e /tmp/persist && test ! -r ${temporary}/ses_one/persist && touch /tmp/session-two'
        if ${localPackages.sandbox-runtime}/bin/srt --settings /run/sandbox-fixture/missing.json \
          -- ${pkgs.coreutils}/bin/touch ${workspace}/must-not-run > missing.log 2>&1; then
          echo 'SRT accepted a missing mandatory seccomp helper' >&2
          exit 1
        fi
        test ! -e must-not-run
        ${pkgs.gnugrep}/bin/grep -Ei 'seccomp|apply-seccomp' missing.log
      '';
    };
  };
  testScript = ''
    import shlex

    machine.start()
    machine.wait_for_unit("sandbox-fixture.service")
    machine.wait_for_open_port(8080)
    machine.wait_for_open_port(8081)
    machine.wait_for_open_port(443)
    # Both public and forbidden destinations are genuinely reachable on the host.
    for host in ["93.184.216.34", "127.0.0.1", "10.55.0.1", "169.254.169.254"]:
        machine.succeed(f"curl --noproxy '*' --fail http://{host}:8081/health")
    machine.succeed("systemctl start --no-block sandbox-probe.service")
    machine.wait_until_succeeds("test -e ${workspace}/ready")
    machine.succeed("${python} disclose ${workspace} ${temporary}")
    machine.wait_for_unit("sandbox-probe.service")
    machine.succeed("test -f ${temporary}/ses_one/persist && test -f ${temporary}/ses_two/session-two")
    # Check cleanup after all successful invocations and secret masking outside
    # the child, so the real token never appears in its test source or arguments.
    machine.succeed("${python} verify ${workspace} ${temporary}")
    events = machine.succeed("cat /run/sandbox-fixture/events").splitlines()
    assert sorted(events) == sorted(["github.com authenticated", "api.github.com authenticated"]), events
    # A nonzero workload exit must also remove its per-call broker directory.
    command = "cd ${workspace} && OPENCODE_SESSION_ID=ses_error ${wrapper} -c 'exit 23'"
    status, _ = machine.execute("runuser -u ${settings.accounts.bot.name} -- sh -c " + shlex.quote(command))
    assert status == 23, status
    machine.succeed("${python} verify ${workspace} ${temporary}")
  '';
}
