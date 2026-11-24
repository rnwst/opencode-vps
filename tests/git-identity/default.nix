{ pkgs, home-manager }:
let
  settings = {
    workspacesRoot = "/build/workspaces";
    accounts = {
      admin = {
        name = "test-admin";
        git = {
          name = "Test Admin";
          email = "test-admin@example.com";
        };
      };
      bot = {
        name = "test-bot";
        git = {
          name = "Test Bot";
          email = "test-bot@example.com";
        };
      };
    };
  };
  home = home-manager.lib.homeManagerConfiguration {
    inherit pkgs;
    extraSpecialArgs = { inherit settings; };
    modules = [
      ../../hosts/opencode/home.nix
      {
        # Exercise the production Git module without installing development tools.
        disabledModules = [ ../../hosts/opencode/development-home.nix ];
        programs.git.enable = true;
      }
    ];
  };
in
pkgs.runCommand "git-identity-tests"
  {
    nativeBuildInputs = [ pkgs.git ];
  }
  ''
    export HOME="$TMPDIR/home"
    export GIT_CONFIG_NOSYSTEM=1
    export GIT_CONFIG_GLOBAL=${home.config.xdg.configFile."git/config".source}
    mkdir -p "$HOME"

    check_commit() {
      local repository=$1 author=$2
      git -C "$repository" commit --allow-empty -m identity
      actual=$(git -C "$repository" log -1 --format='%an <%ae>|%cn <%ce>')
      expected="$author|Test Bot <test-bot@example.com>"
      if [[ "$actual" != "$expected" ]]; then
        printf 'Identity mismatch in %s:\nexpected: %s\nactual: %s\n' \
          "$repository" "$expected" "$actual" >&2
        exit 1
      fi
    }

    # Include ordering must select the bot even though .tasks matches both rules.
    for name in manual .tasks/task-example .tasks-like; do
      repository="${settings.workspacesRoot}/$name"
      git init --initial-branch=main "$repository"
      author='Test Admin <test-admin@example.com>'
      if [[ "$name" == .tasks/* ]]; then
        author='Test Bot <test-bot@example.com>'
      fi
      check_commit "$repository" "$author"
      mkdir "$repository/subdirectory"
      check_commit "$repository/subdirectory" "$author"
    done

    # Existing repositories pick up the defaults without local configuration.
    repository="${settings.workspacesRoot}/existing"
    GIT_CONFIG_GLOBAL=/dev/null git init --initial-branch=main "$repository"
    check_commit "$repository" 'Test Admin <test-admin@example.com>'

    # A sibling directory sharing the workspace prefix must not match.
    repository="${settings.workspacesRoot}-other/repository"
    git init --initial-branch=main "$repository"
    check_commit "$repository" 'Test Bot <test-bot@example.com>'

    # Linked worktrees use the main repository's Git directory for matching.
    git -C "${settings.workspacesRoot}/manual" worktree add -b linked "$TMPDIR/linked"
    check_commit "$TMPDIR/linked" 'Test Admin <test-admin@example.com>'

    repository="${settings.workspacesRoot}/manual"
    git -C "$repository" config author.name 'Local Author'
    git -C "$repository" config author.email local@example.com
    git -C "$repository" config user.name 'Local User'
    git -C "$repository" config user.email user@example.com
    check_commit "$repository" 'Local Author <local@example.com>'

    touch "$out"
  ''
