{
  localPackages,
  pkgs,
}:
let
  fakeGitHub = pkgs.writeText "fake-github.py" (builtins.readFile ./fake-github.py);
  testSettings = {
    opencodePort = 4096;
    workspacesRoot = "/srv/opencode/workspaces";
    githubBridge = {
      enable = true;
      dryRun = false;
      agent = "build";
      model = {
        providerID = "opencode";
        modelID = "big-pickle";
      };
      maxConcurrentTasks = 2;
      minimumFreePercent = 15;
      retentionDays = 30;
      canonicalRoot = "/var/lib/opencode-task-bases";
      stateRoot = "/var/lib/opencode-github-bridge";
    };
    secrets = {
      githubToken = "/var/lib/opencode-secrets/github-token";
      serverPassword = "/var/lib/opencode-secrets/server-password";
      githubControllerId = "/var/lib/opencode-secrets/github-controller-id";
    };
  };
  testWorkspace = import ../../pkgs/opencode-workspace {
    inherit pkgs;
    githubApiUrl = "http://127.0.0.1:4080";
    settings = testSettings;
    sandboxExec = localPackages.sandbox-exec;
    testMode = true;
  };
  testBridge = import ../../pkgs/github-bridge {
    inherit pkgs;
    settings = testSettings;
    workspaceManager = testWorkspace;
  };
  testLocalPackages = localPackages // {
    github-bridge = testBridge;
    opencode-workspace = testWorkspace;
  };
in
pkgs.testers.nixosTest {
  name = "github-bridge";

  nodes.machine =
    { pkgs, ... }:
    {
      imports = [ ../../modules/github-bridge.nix ];
      _module.args = {
        localPackages = testLocalPackages;
        settings = testSettings;
      };

      virtualisation.emptyDiskImages = [ 1024 ];

      users = {
        groups.agent-workspaces = { };
        users = {
          rnwst-admin = {
            isSystemUser = true;
            group = "agent-workspaces";
          };
          rnwst-bot = {
            isSystemUser = true;
            group = "agent-workspaces";
            home = "/var/lib/rnwst-bot";
            createHome = true;
          };
        };
      };

      environment.systemPackages = [
        testLocalPackages.opencode-workspace
        testLocalPackages.sandbox-exec
        pkgs.btrfs-progs
        pkgs.curl
        pkgs.git
        pkgs.jq
      ];

      systemd.services = {
        test-opencode = {
          description = "OpenCode integration-test server";
          wantedBy = [ "multi-user.target" ];
          environment = {
            HOME = "/var/lib/rnwst-bot";
            OPENCODE_CONFIG_CONTENT = builtins.toJSON {
              autoupdate = false;
              share = "disabled";
            };
            OPENCODE_DISABLE_PROJECT_CONFIG = "1";
            OPENCODE_SERVER_PASSWORD = "test-password";
            XDG_CACHE_HOME = "/var/lib/rnwst-bot/.cache";
            XDG_CONFIG_HOME = "/var/lib/rnwst-bot/.config";
            XDG_DATA_HOME = "/var/lib/rnwst-bot/.local/share";
          };
          serviceConfig = {
            User = "rnwst-bot";
            Group = "agent-workspaces";
            ExecStart = "${testLocalPackages.opencode}/bin/opencode serve --pure --hostname 127.0.0.1 --port 4096";
            Restart = "on-failure";
          };
        };

        fake-github = {
          description = "Side-effect-free GitHub API fixture";
          wantedBy = [ "multi-user.target" ];
          path = [
            pkgs.git
            pkgs.python3
          ];
          serviceConfig = {
            ExecStart = "${pkgs.python3}/bin/python3 ${fakeGitHub}";
            Restart = "on-failure";
          };
        };

        github-bridge.environment = {
          GITHUB_BRIDGE_API_URL = "http://127.0.0.1:4080";
          GITHUB_BRIDGE_GRAPHQL_URL = "http://127.0.0.1:4080/graphql";
        };
      };
    };

  testScript = ''
    import hashlib
    import json
    import urllib.parse

    machine.start()
    machine.wait_for_unit("test-opencode.service")
    machine.wait_for_open_port(4096)
    machine.wait_for_unit("fake-github.service")
    machine.wait_for_open_port(4080)
    machine.succeed(
      "curl --fail --silent --user opencode:test-password "
      "http://127.0.0.1:4096/global/health | jq -e .healthy"
    )

    machine.succeed("mkfs.btrfs -f /dev/vdb")
    machine.succeed("mkdir -p /srv/opencode")
    machine.succeed("mount -o compress=zstd:1,noatime,space_cache=v2 /dev/vdb /srv/opencode")
    machine.succeed("mkdir -p /srv/opencode/workspaces /srv/opencode/canonical /var/lib/opencode-task-bases")
    machine.succeed("mount --bind /srv/opencode/canonical /var/lib/opencode-task-bases")
    machine.succeed("mkdir -p /var/lib/opencode-secrets /srv/git")
    machine.succeed("printf test-token > /var/lib/opencode-secrets/github-token")
    machine.succeed("printf test-password > /var/lib/opencode-secrets/server-password")
    machine.succeed("printf 100 > /var/lib/opencode-secrets/github-controller-id")

    machine.succeed("git init --bare /srv/git/repo.git")
    machine.succeed("git init --initial-branch=main /tmp/seed")
    machine.succeed("git -C /tmp/seed config user.name Test")
    machine.succeed("git -C /tmp/seed config user.email test@example.com")
    machine.succeed("printf first > /tmp/seed/version")
    machine.succeed("git -C /tmp/seed add version")
    machine.succeed("git -C /tmp/seed commit -m first")
    machine.succeed("git -C /tmp/seed remote add origin file:///srv/git/repo.git")
    machine.succeed("git -C /tmp/seed push origin main")
    machine.succeed(
      "git --git-dir=/srv/git/repo.git update-ref refs/pull/1/head refs/heads/main"
    )

    workspace_env = "OPENCODE_GITHUB_API_URL=http://127.0.0.1:4080 OPENCODE_WORKSPACE_TEST_MODE=1"
    machine.succeed(f"{workspace_env} opencode-workspace create owner/repo cloned-one")
    machine.succeed("test $(cat /srv/opencode/workspaces/cloned-one/version) = first")
    machine.succeed(
      "runuser -u rnwst-bot -- bash -c 'cd /srv/opencode/workspaces/cloned-one && "
      "opencode-sandbox-exec -c \"test ! -r /var/lib/opencode-task-bases/738/version\"'"
    )

    machine.succeed("printf second > /tmp/seed/version")
    machine.succeed("git -C /tmp/seed commit -am second")
    machine.succeed("git -C /tmp/seed push origin main")
    machine.succeed(f"{workspace_env} opencode-workspace create owner/repo cloned-two")
    machine.succeed("test $(cat /srv/opencode/workspaces/cloned-two/version) = second")
    machine.succeed("test $(cat /srv/opencode/workspaces/cloned-one/version) = first")

    machine.succeed("touch /tmp/repo-push-denied")
    machine.succeed(
      f"{workspace_env} opencode-workspace prepare-task task-bbbbbbbbbbbbbbbb "
      "owner/repo implement 1 pull/1 > /tmp/fork-task.json"
    )
    machine.succeed("jq -e '.repository_id == \"738\"' /tmp/fork-task.json")
    machine.succeed(
      "test $(runuser -u rnwst-bot -- git -C "
      "/srv/opencode/workspaces/.tasks/task-bbbbbbbbbbbbbbbb remote get-url origin) "
      "= https://github.com/bot/repo.git"
    )
    machine.succeed("opencode-workspace remove-task task-bbbbbbbbbbbbbbbb")
    machine.succeed("rm /tmp/repo-push-denied")

    machine.succeed("opencode-workspace init alpha")
    machine.succeed("btrfs subvolume show /srv/opencode/workspaces/alpha")
    machine.succeed("touch /srv/opencode/workspaces/alpha/original")
    machine.succeed(
      "btrfs subvolume snapshot /srv/opencode/workspaces/alpha "
      "/srv/opencode/workspaces/beta"
    )
    machine.succeed("touch /srv/opencode/workspaces/beta/changed")
    machine.fail("test -e /srv/opencode/workspaces/alpha/changed")
    machine.succeed("test -e /srv/opencode/workspaces/beta/original")
    machine.fail(
      "runuser -u rnwst-bot -- bash -c 'cd /srv/opencode/workspaces && opencode-sandbox-exec -c true'"
    )
    machine.succeed(
      "runuser -u rnwst-bot -- bash -c 'cd /srv/opencode/workspaces/alpha && "
      "opencode-sandbox-exec -c \"test ! -r ../beta/changed\"'"
    )
    machine.succeed("mkdir -p /srv/opencode/workspaces/.tasks/task-aaaaaaaaaaaaaaaa")
    machine.fail(
      "opencode-workspace restore-task task-aaaaaaaaaaaaaaaa owner/repo answer 1 default"
    )
    machine.succeed("rmdir /srv/opencode/workspaces/.tasks/task-aaaaaaaaaaaaaaaa")

    machine.succeed("opencode-workspace init local-project")
    machine.succeed(f"{workspace_env} opencode-workspace set-remote local-project owner/repo")
    machine.succeed(
      "test $(runuser -u rnwst-bot -- git -C /srv/opencode/workspaces/local-project remote get-url origin) "
      "= file:///srv/git/repo.git"
    )

    machine.succeed("opencode-workspace init removable-project")
    machine.succeed(
      "runuser -u rnwst-bot -- bash -c 'cd \"$1\"; exec \"$2\" -c \"$3\"' bash "
      "/srv/opencode/workspaces/removable-project opencode-sandbox-exec "
      "'test \"$TMPDIR\" = /tmp/claude; "
      "temporary=$(mktemp); test -f \"$temporary\"; rm \"$temporary\"'"
    )
    machine.succeed("opencode-workspace remove removable-project")
    machine.fail("test -e /srv/opencode/workspaces/removable-project")

    machine.succeed("opencode-workspace remove cloned-one > /tmp/remove-output 2>&1")
    machine.succeed("grep -q 'fetching remote refs into a temporary repository' /tmp/remove-output")
    machine.succeed("grep -q 'Removed workspace: /srv/opencode/workspaces/cloned-one' /tmp/remove-output")
    machine.fail("grep -Eq 'Cloning into|^From |^remote:|^error:' /tmp/remove-output")

    machine.succeed("opencode-workspace init force-project")
    machine.succeed(
      "runuser -u rnwst-bot -- git -C /srv/opencode/workspaces/force-project "
      "-c user.name=Test -c user.email=test@example.com commit --allow-empty -m local-only"
    )
    machine.fail("opencode-workspace remove force-project")
    machine.succeed("opencode-workspace remove force-project --force > /tmp/force-output")
    machine.succeed("grep -q 'Skipping cleanliness and remote commit verification' /tmp/force-output")
    machine.fail("test -e /srv/opencode/workspaces/force-project")

    machine.succeed("opencode-workspace init dirty-force-project")
    machine.succeed("touch /srv/opencode/workspaces/dirty-force-project/untracked")
    machine.succeed("opencode-workspace remove dirty-force-project --force")
    machine.fail("test -e /srv/opencode/workspaces/dirty-force-project")

    machine.succeed("opencode-workspace init public-project")
    machine.succeed("runuser -u rnwst-bot -- git -C /srv/opencode/workspaces/public-project -c user.name=Test -c user.email=test@example.com commit --allow-empty -m initial")
    machine.succeed(f"{workspace_env} opencode-workspace publish public-project owner/public-project")
    machine.succeed("git --git-dir=/srv/git/public-project.git rev-parse refs/heads/main")
    machine.succeed("opencode-workspace init private-project")
    machine.succeed(f"{workspace_env} opencode-workspace publish private-project owner/private-project --visibility private")
    machine.succeed("jq -s -e 'map(.body.private) == [false, true]' /tmp/fake-github-events.jsonl")
    machine.succeed("opencode-workspace list > /tmp/workspace-list")
    machine.succeed("grep -q public-project /tmp/workspace-list")

    directory = urllib.parse.quote("/srv/opencode/workspaces/alpha", safe="")
    created = machine.succeed(
      "curl --fail --silent --user opencode:test-password "
      "--header 'Content-Type: application/json' "
      "--data '{\"title\":\"Bridge test session\"}' "
      f"'http://127.0.0.1:4096/session?directory={directory}'"
    )
    session_id = json.loads(created)["id"]
    machine.succeed(
      "curl --fail --silent --user opencode:test-password "
      "--header 'Content-Type: application/json' "
      "--data '{\"noReply\":true,\"parts\":[{\"type\":\"text\","
      "\"text\":\"deterministic bridge prompt\"}]}' "
      f"'http://127.0.0.1:4096/session/{session_id}/prompt_async?directory={directory}'"
    )
    machine.wait_until_succeeds(
      "curl --fail --silent --user opencode:test-password "
      f"'http://127.0.0.1:4096/session/{session_id}/message?directory={directory}' "
      "| jq -e 'map(.parts[]? | select(.type == \"text\") | .text) "
      "| index(\"deterministic bridge prompt\")'"
    )
    machine.succeed(
      "curl --fail --silent --user opencode:test-password --request POST "
      f"'http://127.0.0.1:4096/session/{session_id}/abort?directory={directory}'"
    )
    machine.succeed(
      "curl --fail --silent --user opencode:test-password --request DELETE "
      f"'http://127.0.0.1:4096/session/{session_id}?directory={directory}'"
    )

    machine.succeed("systemctl start github-bridge.service")
    machine.succeed("test -s /var/lib/opencode-github-bridge/bridge.sqlite")
    machine.succeed("github-bridge status")
    machine.succeed("touch /tmp/enable-github-notification")
    machine.succeed(
      "${pkgs.python3}/bin/python3 -c \"import sqlite3; db=sqlite3.connect('/var/lib/opencode-github-bridge/bridge.sqlite'); "
      "db.execute('update metadata set value=0 where key=\\\"next_poll_at\\\"'); db.commit()\""
    )
    machine.succeed("systemctl start github-bridge.service")
    machine.succeed("github-bridge status > /tmp/bridge-status")
    task_id = "task-owner-repo-issue-1-" + hashlib.sha256(
      b"issue-event:601"
    ).hexdigest()[:12]
    machine.succeed(f"grep -q {task_id} /tmp/bridge-status")
    machine.succeed(f"btrfs subvolume show /srv/opencode/workspaces/.tasks/{task_id}")
    machine.succeed("test $(find /srv/opencode/workspaces/.tasks -mindepth 1 -maxdepth 1 -type d | wc -l) = 1")
    task_directory = urllib.parse.quote(
      f"/srv/opencode/workspaces/.tasks/{task_id}", safe=""
    )
    machine.wait_until_succeeds(
      "curl --fail --silent --user opencode:test-password "
      f"'http://127.0.0.1:4096/session?directory={task_directory}' "
      "| jq -e '.[0].title | contains(\"owner/repo#1 implement\")'"
    )
    machine.wait_until_succeeds(
      "curl --fail --silent --user opencode:test-password "
      f"'http://127.0.0.1:4096/session?directory={task_directory}' "
      "| jq -er '.[0].id' > /tmp/task-session-id"
    )
    machine.wait_until_succeeds(
      "session=$(cat /tmp/task-session-id); "
      "curl --fail --silent --user opencode:test-password "
      f"\"http://127.0.0.1:4096/session/$session/message?directory={task_directory}\" "
      "| jq -e 'map(.parts[]? | select(.type == \"text\") | .text) "
      "| any(startswith(\"## GitHub Task\\n\\n- Action:\"))'"
    )
  '';
}
