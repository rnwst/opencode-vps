{
  pkgs,
  pkgsUnstable,
  settings,
}:
let
  inherit (pkgs) julia;
  botHome = "/home/${settings.accounts.bot.name}";
  opencode = import ./opencode.nix { inherit pkgsUnstable; };
  playwright-mcp = import ./playwright-mcp.nix { inherit pkgs; };
  previewCfg = import ../config/previews.nix {
    inherit settings;
    inherit (pkgs) lib;
  };
  opencode-preview = import ./opencode-preview {
    inherit pkgs settings;
    sandboxExec = sandbox-exec;
    playwrightMcp = playwright-mcp;
  };

  # SRT's generic policy creates temporary mount points for repository files
  # such as .gitmodules. This host treats repository configuration as source,
  # while retaining SRT's separate .git/config and .git/hooks protections.
  sandbox-runtime = pkgsUnstable.sandbox-runtime.overrideAttrs (oldAttrs: {
    patches = (oldAttrs.patches or [ ]) ++ [
      ./sandbox-runtime-host-policy.patch
      ./sandbox-runtime-hardening.patch
      ./sandbox-runtime-network.patch
    ];
    nativeBuildInputs = (oldAttrs.nativeBuildInputs or [ ]) ++ [ pkgsUnstable.xxd ];
    buildInputs = (oldAttrs.buildInputs or [ ]) ++ [ pkgsUnstable.libseccomp ];
    # The upstream npm build only compiles TypeScript, not the native helper.
    preBuild = (oldAttrs.preBuild or "") + ''
      helper_dir=vendor/seccomp/${if pkgs.stdenv.hostPlatform.isx86_64 then "x64" else "arm64"}
      mkdir -p "$helper_dir"
      $CC -O2 -Wall -Wextra vendor/seccomp-src/seccomp-unix-block.c \
        -lseccomp -o generate-seccomp
      ./generate-seccomp unix-block.bpf ${
        if pkgs.stdenv.hostPlatform.isx86_64 then "x86_64" else "aarch64"
      }
      xxd -i -n unix_block_bpf unix-block.bpf > "$helper_dir/unix-block-bpf.h"
      $CC -O2 -Wall -Wextra -I "$helper_dir" vendor/seccomp-src/apply-seccomp.c \
        -o "$helper_dir/apply-seccomp"
      rm generate-seccomp unix-block.bpf "$helper_dir/unix-block-bpf.h"
    '';
    postInstall = (oldAttrs.postInstall or "") + ''
      test -x "$out/lib/node_modules/@anthropic-ai/sandbox-runtime/$helper_dir/apply-seccomp"
    '';
  });

  fishCompletionGenerator =
    pkgs.runCommandLocal "fish-completion-generator"
      {
        nativeBuildInputs = [ pkgsUnstable.fish ];
      }
      ''
        mkdir -p $out/share/fish/tools
        fish --no-config -c 'status get-file tools/create_manpage_completions.py' \
          > $out/share/fish/tools/create_manpage_completions.py
      '';

  # Stable NixOS and Home Manager modules expect the pre-4.8 generator path.
  fish = pkgs.symlinkJoin {
    name = "fish-${pkgsUnstable.fish.version}";
    paths = [
      pkgsUnstable.fish
      fishCompletionGenerator
    ];
    inherit (pkgsUnstable.fish) meta version;
    passthru = pkgsUnstable.fish.passthru;
  };

  opencode-workspace = import ./opencode-workspace {
    inherit pkgs settings;
    sandboxExec = sandbox-exec;
  };
  github-bridge = import ./github-bridge {
    inherit pkgs settings;
    workspaceManager = opencode-workspace;
  };

  headlessXdgOpen = pkgs.writeShellApplication {
    name = "xdg-open";
    text = "exit 0";
  };

  jetls = pkgs.writeShellApplication {
    name = "jetls";
    runtimeInputs = [ julia ];
    text = ''
      depot="''${JULIA_DEPOT_PATH:-$HOME/.julia}"
      app="$depot/bin/jetls"

      if [[ ! -x "$app" ]]; then
        echo "Installing pinned JETLS 2026-02-27 into $depot" >&2
        JULIA_DEPOT_PATH="$depot" julia --startup-file=no --history-file=no \
          -e 'using Pkg; Pkg.Apps.add(; url="https://github.com/aviatesk/JETLS.jl", rev="2026-02-27")'
      fi

      exec "$app" "$@"
    '';
  };

  jlfmt = pkgs.writeShellApplication {
    name = "jlfmt";
    runtimeInputs = [ julia ];
    text = ''
      depot="''${JULIA_DEPOT_PATH:-$HOME/.julia}"
      app="$depot/bin/jlfmt"

      if [[ ! -x "$app" ]]; then
        echo "Installing pinned JuliaFormatter 2.13.0 into $depot" >&2
        JULIA_DEPOT_PATH="$depot" julia --startup-file=no --history-file=no \
          -e 'using Pkg; Pkg.Apps.add(; name="JuliaFormatter", version="2.13.0")'
      fi

      exec "$app" "$@"
    '';
  };

  opencode-git = pkgs.writeShellApplication {
    name = "opencode-git";
    runtimeInputs = with pkgs; [
      coreutils
      git
      util-linux
    ];
    text = ''
      if (( EUID != 0 )); then
        echo "opencode-git must be run with sudo" >&2
        exit 77
      fi
      if [[ $# -eq 0 ]]; then
        echo "usage: sudo opencode-git GIT_ARGUMENT..." >&2
        exit 64
      fi
      if [[ ! -r ${settings.secrets.githubToken} ]]; then
        echo "GitHub token is unavailable at ${settings.secrets.githubToken}" >&2
        exit 66
      fi

      token="$(<${settings.secrets.githubToken})"
      # shellcheck disable=SC2016
      credential_helper='!f() { if [ "$1" = get ]; then printf "%s\n" "username=x-access-token" "password=$OPENCODE_GITHUB_TOKEN"; fi; }; f'

      cd ${settings.workspacesRoot}
      exec runuser --user ${pkgs.lib.escapeShellArg settings.accounts.bot.name} -- env \
        GIT_TERMINAL_PROMPT=0 \
        HOME=${pkgs.lib.escapeShellArg botHome} \
        OPENCODE_GITHUB_TOKEN="$token" \
        XDG_CONFIG_HOME=${pkgs.lib.escapeShellArg "${botHome}/.config"} \
        ${pkgs.git}/bin/git \
        -c credential.https://github.com.helper= \
        -c credential.https://github.com.helper="$credential_helper" \
        "$@"
    '';
  };

  sandbox-exec = pkgs.writeShellApplication {
    name = "opencode-sandbox-exec";
    runtimeInputs = with pkgs; [
      bash
      bubblewrap
      coreutils
      git
      jq
      sandbox-runtime
      ripgrep
      socat
    ];
    text = ''
      mode="exec"
      if [[ $# -eq 0 ]]; then
        mode="shell"
        set -- ${pkgs.bashInteractive}/bin/bash --noprofile --norc -i
      elif [[ "''${1:-}" == "-c" ]]; then
        mode="shell"
        shift
        [[ $# -eq 1 ]] || { echo "usage: opencode-sandbox-exec -c COMMAND" >&2; exit 64; }
        command_text="$1"
        set -- ${pkgs.bash}/bin/bash -c "$command_text"
      else
        [[ "''${1:-}" == "--" ]] && shift
        [[ $# -gt 0 ]] || { echo "usage: opencode-sandbox-exec -- COMMAND [ARG ...]" >&2; exit 64; }
      fi

      cwd="$(pwd -P)"
      root="$(${pkgs.git}/bin/git -C "$cwd" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$cwd")"
      root="$(realpath "$root")"
      case "$root/" in
        ${settings.workspacesRoot}/?*/) ;;
        *) echo "refusing to run outside ${settings.workspacesRoot}: $root" >&2; exit 77 ;;
      esac

      relative="''${root#${settings.workspacesRoot}/}"
      case "$relative" in
        .tasks/*)
          workspace_name="''${relative#.tasks/}"
          [[ "$workspace_name" != */* ]] || { echo "invalid automated workspace root: $root" >&2; exit 77; }
          workspace_tmp="${settings.workspacesTmpRoot}/.tasks/$workspace_name"
          ;;
        *)
          [[ "$relative" != */* ]] || { echo "invalid manual workspace root: $root" >&2; exit 77; }
          workspace_tmp="${settings.workspacesTmpRoot}/$relative"
          ;;
      esac
      session_id="''${OPENCODE_SESSION_ID:-ses_cli}"
      [[ "$session_id" =~ ^ses_[A-Za-z0-9]+$ ]] || { echo "invalid OpenCode session ID" >&2; exit 77; }
      [[ -d "$workspace_tmp" && ! -L "$workspace_tmp" ]] || {
        echo "temporary root is unavailable for workspace: $root" >&2
        exit 77
      }
      session_tmp="$workspace_tmp/$session_id"
      if [[ -e "$session_tmp" ]]; then
        [[ -d "$session_tmp" && ! -L "$session_tmp" ]] || {
          echo "invalid session temporary directory: $session_tmp" >&2
          exit 77
        }
      else
        install -d -m 0700 "$session_tmp"
      fi
      chmod 0700 "$session_tmp"
      install -d -m 0700 "$session_tmp/claude"

      # SRT's CA keys and settings must never be created in workload-visible /tmp.
      # This backing directory is hidden by denyRead; SRT rebinds only public
      # trust files and the sockets needed by its trusted networking helpers.
      # Mount it at /var/tmp below to avoid Unix socket path-length limits.
      if [[ -n "''${OPENCODE_PREVIEW_RUNTIME_ID:-}" ]]; then
        [[ "$OPENCODE_PREVIEW_RUNTIME_ID" =~ ^[a-f0-9]{24}$ ]] || {
          echo "invalid managed runtime ID" >&2; exit 77;
        }
        broker_tmp="${previewCfg.runtimeRoot}/broker/$OPENCODE_PREVIEW_RUNTIME_ID"
        mkdir -m 0700 -- "$broker_tmp"
      else
        broker_tmp="$(mktemp -d "$workspace_tmp/.srt-broker.XXXXXXXX")"
      fi
      settings_file="$broker_tmp/settings.json"
      trap 'rm -rf -- "$broker_tmp"' EXIT
      seccomp="${sandbox-runtime}/lib/node_modules/@anthropic-ai/sandbox-runtime/vendor/seccomp/${
        if pkgs.stdenv.hostPlatform.isx86_64 then "x64" else "arm64"
      }/apply-seccomp"
      [[ -x "$seccomp" ]] || { echo "required sandbox seccomp helper is unavailable" >&2; exit 69; }

      if [[ "$mode" == "shell" && -n "''${CREDENTIALS_DIRECTORY:-}" && -r "$CREDENTIALS_DIRECTORY/github-token" ]]; then
        GH_TOKEN="$(<"$CREDENTIALS_DIRECTORY/github-token")"
        # Select the writable fork for bare pushes without persisting branch
        # metadata in the protected repository config.
        GIT_CONFIG_COUNT=5
        GIT_CONFIG_KEY_0=http.https://github.com/.extraHeader
        GIT_CONFIG_VALUE_0="Authorization: Basic $(printf 'x-access-token:%s' "$GH_TOKEN" | base64 --wrap=0)"
        GIT_CONFIG_KEY_1=credential.helper
        GIT_CONFIG_VALUE_1=
        GIT_CONFIG_KEY_2=push.default
        GIT_CONFIG_VALUE_2=current
        GIT_CONFIG_KEY_3=remote.pushDefault
        GIT_CONFIG_VALUE_3=origin
        GIT_CONFIG_KEY_4=push.autoSetupRemote
        GIT_CONFIG_VALUE_4=false
        export GH_TOKEN
        export GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0
        export GIT_CONFIG_KEY_1 GIT_CONFIG_VALUE_1 GIT_CONFIG_KEY_2 GIT_CONFIG_VALUE_2
        export GIT_CONFIG_KEY_3 GIT_CONFIG_VALUE_3 GIT_CONFIG_KEY_4 GIT_CONFIG_VALUE_4
        export GIT_TERMINAL_PROMPT=0
      fi

      unset OPENCODE_SERVER_PASSWORD
      export BUN_INSTALL_CACHE_DIR=/tmp/opencode-cache/bun
      export CARGO_HOME=/tmp/opencode-cache/cargo
      export DENO_DIR=/tmp/opencode-cache/deno
      export GH_CONFIG_DIR=/tmp/opencode-gh-config
      export GOPATH=/tmp/opencode-cache/go
      export GRADLE_USER_HOME=/tmp/opencode-cache/gradle
      export JULIA_DEPOT_PATH=/tmp/opencode-cache/julia
      export MAVEN_OPTS="''${MAVEN_OPTS:-} -Dmaven.repo.local=/tmp/opencode-cache/maven"
      export npm_config_cache=/tmp/opencode-cache/npm
      export PIP_CACHE_DIR=/tmp/opencode-cache/pip
      export UV_CACHE_DIR=/tmp/opencode-cache/uv
      export XDG_CACHE_HOME=/tmp/opencode-cache/xdg

      jq -n \
        --arg root "$root" \
        --arg home "$HOME" \
        --arg workspaces_root "${settings.workspacesRoot}" \
        --arg workspaces_tmp_root "${settings.workspacesTmpRoot}" \
        --arg canonical_root "${settings.githubBridge.canonicalRoot}" \
        --arg bridge_state "${settings.githubBridge.stateRoot}" \
        --arg opencode_auth "$HOME/.local/share/opencode/auth.json" \
        --arg ssh_dir "$HOME/.ssh" \
        --arg credentials_dir "''${CREDENTIALS_DIRECTORY:-/run/credentials}" \
        --arg bwrap "${pkgs.bubblewrap}/bin/bwrap" \
        --arg rg "${pkgs.ripgrep}/bin/rg" \
        --arg socat "${pkgs.socat}/bin/socat" \
        --arg seccomp "$seccomp" \
        --arg preview_runtime "${previewCfg.runtimeRoot}" \
        '{
          filesystem: {
            denyRead: [
              $home,
              $opencode_auth,
              $ssh_dir,
              $credentials_dir,
              $workspaces_root,
              $workspaces_tmp_root,
              $canonical_root,
              $bridge_state,
              $preview_runtime,
              "/var/tmp"
            ],
            allowRead: [$root, ($home + "/.config/git"), ($home + "/.gitconfig")],
            allowWrite: [$root, "/tmp"],
            denyWrite: []
          },
          network: {
            allowedDomains: [
              "api.github.com",
              "github.com",
              "*.github.com",
              "githubusercontent.com",
              "*.githubusercontent.com",
              "npmjs.org",
              "*.npmjs.org",
              "nodejs.org",
              "*.nodejs.org",
              "pypi.org",
              "*.pypi.org",
              "pythonhosted.org",
              "*.pythonhosted.org",
              "crates.io",
              "*.crates.io",
              "rust-lang.org",
              "*.rust-lang.org",
              "rustup.rs",
              "*.rustup.rs",
              "go.dev",
              "*.go.dev",
              "golang.org",
              "*.golang.org",
              "goproxy.io",
              "*.goproxy.io",
              "julialang.org",
              "*.julialang.org",
              "pkg.julialang.org",
              "*.pkg.julialang.org",
              "maven.org",
              "*.maven.org",
              "gradle.org",
              "*.gradle.org",
              "nixos.org",
              "*.nixos.org",
              "cache.nixos.org",
              "docker.io",
              "*.docker.io",
              "docker.com",
              "*.docker.com",
              "bun.sh",
              "*.bun.sh",
              "deno.land",
              "*.deno.land"
            ],
            deniedDomains: ["*:22"],
            deniedDomainReasons: {
              "*:22": "SSH is blocked in agent commands; use GitHub over HTTPS with the masked GH_TOKEN."
            },
            # Explicit denies still apply, while unmatched public dependency
            # hosts are reachable without an interactive approval callback.
            strictAllowlist: false,
            allowLocalBinding: false,
            tlsTerminate: {}
          },
          credentials: {
            envVars: [
              {
                name: "GH_TOKEN",
                mode: "mask",
                injectHosts: ["api.github.com", "github.com"]
              },
              {
                name: "GIT_CONFIG_VALUE_0",
                mode: "mask",
                extract: "^Authorization: Basic (.+)$",
                onExtractNoMatch: "error",
                injectHosts: ["github.com"]
              }
            ]
          },
          bwrapPath: $bwrap,
          socatPath: $socat,
          seccomp: { applyPath: $seccomp },
          ripgrep: { command: $rg },
          git: { safeDirectories: [$root] }
        }' > "$settings_file"

      ${pkgs.bubblewrap}/bin/bwrap \
        --die-with-parent \
        --new-session \
        --ro-bind / / \
        --dev-bind /dev /dev \
        --proc /proc \
        --bind "$root" "$root" \
        --bind "$session_tmp" /tmp \
        --bind "$broker_tmp" /var/tmp \
        --setenv TMPDIR /var/tmp \
        --unsetenv TMP \
        --unsetenv TEMP \
        --chdir "$cwd" \
        -- ${sandbox-runtime}/bin/srt --settings "$settings_file" -- "$@"
    '';
  };

  agent-ci = pkgs.writeShellApplication {
    name = "agent-ci";
    runtimeInputs = with pkgs; [
      act
      coreutils
      docker-client
      rsync
    ];
    text = ''
      source_dir=""
      workflow=""
      job=""
      event="push"
      max_time="45m"

      while [[ $# -gt 0 ]]; do
        case "$1" in
          --source) source_dir="$2"; shift 2 ;;
          --workflow) workflow="$2"; shift 2 ;;
          --job) job="$2"; shift 2 ;;
          --event) event="$2"; shift 2 ;;
          --timeout) max_time="$2"; shift 2 ;;
          *) echo "unknown argument: $1" >&2; exit 64 ;;
        esac
      done

      [[ -n "$source_dir" ]] || { echo "--source is required" >&2; exit 64; }
      [[ "$event" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || { echo "invalid event name" >&2; exit 64; }
      [[ "$max_time" =~ ^[1-9][0-9]{0,4}(s|m|h)$ ]] || { echo "invalid timeout" >&2; exit 64; }
      duration="''${max_time%?}"
      case "$max_time" in
        *h) (( duration <= 2 )) || { echo "timeout must not exceed 2h" >&2; exit 64; } ;;
        *m) (( duration <= 120 )) || { echo "timeout must not exceed 2h" >&2; exit 64; } ;;
        *s) (( duration <= 7200 )) || { echo "timeout must not exceed 2h" >&2; exit 64; } ;;
      esac
      source_dir="$(realpath "$source_dir")"
      case "$source_dir/" in
        ${settings.workspacesRoot}/*/) ;;
        *) echo "source must be below ${settings.workspacesRoot}" >&2; exit 64 ;;
      esac

      if [[ -n "$workflow" ]]; then
        case "$workflow" in
          /*|*..*) echo "workflow must be a safe relative path" >&2; exit 64 ;;
        esac
      fi

      run_dir="$(mktemp -d /var/lib/ci-runner/jobs/job.XXXXXX)"
      job_dir="$run_dir/worktree"
      home_dir="$run_dir/home"
      mkdir -p "$job_dir" "$home_dir"
      trap 'rm -rf "$run_dir"' EXIT

      rsync --archive --delete --safe-links \
        --exclude .direnv/ \
        --exclude node_modules/ \
        --exclude result \
        "$source_dir/" "$job_dir/"

      uid="$(id -u)"
      export HOME="$home_dir"
      export XDG_CACHE_HOME="$home_dir/.cache"
      export XDG_CONFIG_HOME="$home_dir/.config"
      export XDG_DATA_HOME="$home_dir/.local/share"
      export XDG_RUNTIME_DIR="/run/user/$uid"
      export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/docker.sock"
      unset GH_TOKEN GITHUB_TOKEN OPENCODE_SERVER_PASSWORD CREDENTIALS_DIRECTORY

      if [[ ! -S "$XDG_RUNTIME_DIR/docker.sock" ]]; then
        echo "rootless Docker is not ready at $XDG_RUNTIME_DIR/docker.sock" >&2
        exit 69
      fi

      args=(
        "$event"
        --platform ubuntu-latest=catthehacker/ubuntu:act-latest
        --platform ubuntu-22.04=catthehacker/ubuntu:act-22.04
        --platform ubuntu-20.04=catthehacker/ubuntu:act-20.04
      )
      [[ -n "$workflow" ]] && args+=(--workflows "$workflow")
      [[ -n "$job" ]] && args+=(--job "$job")

      cd "$job_dir"
      timeout --foreground "$max_time" ${pkgs.act}/bin/act "''${args[@]}"
    '';
  };

  opencode-server = pkgs.writeShellApplication {
    name = "opencode-server";
    runtimeInputs = [
      headlessXdgOpen
      opencode
    ];
    text = ''
      : "''${CREDENTIALS_DIRECTORY:?systemd credentials are not available}"
      export OPENCODE_SERVER_USERNAME=opencode
      OPENCODE_SERVER_PASSWORD="$(<"$CREDENTIALS_DIRECTORY/server-password")"
      export OPENCODE_SERVER_PASSWORD
      exec ${opencode}/bin/opencode web \
        --hostname 127.0.0.1 \
        --port ${toString settings.opencodePort}
    '';
  };

  opencode-plugin = pkgs.writeText "managed-opencode-plugin.js" ''
    import { randomUUID } from "node:crypto"
    import { realpathSync } from "node:fs"
    import { readFile, rename, rm, unlink, writeFile } from "node:fs/promises"

    const { tool } = await import(
      Bun.resolveSync("@opencode-ai/plugin", ${builtins.toJSON "${botHome}/.config/opencode"}),
    )

    export const ManagedHost = async ({ directory, client }) => {
      let workspaceAllowed = false
      let workspaceTmp
      try {
        const root = realpathSync("${settings.workspacesRoot}")
        const current = realpathSync(directory)
        workspaceAllowed = current.startsWith(root + "/")
        const relative = current.slice(root.length + 1)
        if (relative.startsWith(".tasks/")) {
          const task = relative.slice(".tasks/".length)
          if (task && !task.includes("/")) {
            workspaceTmp = `${settings.workspacesTmpRoot}/.tasks/''${task}`
          }
        } else if (relative && !relative.includes("/")) {
          workspaceTmp = `${settings.workspacesTmpRoot}/''${relative}`
        }
      } catch {}

      // Descendants share their conversation's runtime, never an unrelated
      // session's. Resolve ancestry from OpenCode, not tool-supplied metadata.
      const rootSessionID = async (sessionID) => {
        const seen = new Set()
        while (seen.size < 64) {
          if (typeof sessionID !== "string" || !/^ses_[A-Za-z0-9]+$/.test(sessionID) || seen.has(sessionID)) {
            throw new Error("Invalid OpenCode session ancestry")
          }
          seen.add(sessionID)
          const { data } = await client.session.get({
            path: { id: sessionID },
            query: { directory },
            throwOnError: true,
          })
          if (!data || data.id !== sessionID || data.directory !== directory) {
            throw new Error("OpenCode session does not match the managed workspace")
          }
          if (data.parentID === undefined) return sessionID
          sessionID = data.parentID
        }
        throw new Error("OpenCode session ancestry is too deep")
      }

      const tmpPathKeys = {
        edit: "filePath",
        glob: "path",
        grep: "path",
        read: "filePath",
        write: "filePath",
      }
      const isTmpPath = (value) =>
        typeof value === "string" && (value === "/tmp" || value.startsWith("/tmp/"))

      // Privileged workspace changes cross a file-based request channel so the
      // OpenCode process never receives sudo access or an unmasked GitHub token.
      const managedRequest = async (operation, args, context) => {
        if (!workspaceAllowed || context.directory !== directory) {
          throw new Error("Managed GitHub operations require the current workspace")
        }
        const sessionID = await rootSessionID(context.sessionID)
        const timeout = 300000
        const requestID = randomUUID().replaceAll("-", "")
        const root = "${settings.githubBridge.stateRoot}"
        const temporary = `${"$"}{root}/inbox/.request-${"$"}{requestID}.tmp`
        const request = `${"$"}{root}/inbox/request-${"$"}{requestID}.json`
        const response = `${"$"}{root}/responses/response-${"$"}{requestID}.json`
        const payload = JSON.stringify({
          version: 1,
          operation,
          request_id: requestID,
          session_id: sessionID,
          directory: context.directory,
          expires_at: Date.now() + timeout,
          arguments: args,
        })
        let aborted = context.abort.aborted
        const abort = () => { aborted = true }
        context.abort.addEventListener("abort", abort, { once: true })
        await writeFile(temporary, payload, { flag: "wx", mode: 0o640 })
        await rename(temporary, request)

        try {
          for (let attempt = 0; attempt < timeout / 200; attempt++) {
            if (aborted) throw new Error("Managed GitHub operation cancelled")
            try {
              const result = JSON.parse(await readFile(response, "utf8"))
              await unlink(response)
              if (result.request_id !== requestID) {
                throw new Error("Managed GitHub response ID did not match the request")
              }
              if (!result.ok) throw new Error(result.error || "Managed GitHub operation failed")
              return result.result ?? result
            } catch (error) {
              if (error?.code !== "ENOENT") throw error
            }
            await Bun.sleep(200)
          }
          throw new Error("Timed out waiting for managed GitHub operation")
        } finally {
          context.abort.removeEventListener("abort", abort)
          await unlink(temporary).catch(() => {})
          await unlink(request).catch(() => {})
        }
      }

      return {
        event: async (input) => {
          if (input.event.type !== "session.deleted" || !workspaceTmp) return
          // Deleting a child must not stop its root conversation's runtime.
          const sessionID = input.event.properties.info.id
          if (!/^ses_[A-Za-z0-9]+$/.test(sessionID)) return
          ${pkgs.lib.optionalString previewCfg.enable ''
            const stopped = Bun.spawn([
              "${opencode-preview}/bin/opencode-session-exec", "stop",
              "--session", sessionID, "--directory", directory,
            ], { stdout: "ignore", stderr: "pipe" })
            if (await stopped.exited !== 0) {
              throw new Error("Could not stop the session runtime before removing temporary files")
            }
          ''}
          await rm(`''${workspaceTmp}/''${sessionID}`, { recursive: true, force: true })
        },
        "shell.env": async (input, output) => {
          if (!workspaceAllowed) throw new Error("Shells require a managed workspace")
          if (input.sessionID === undefined) return
          output.env.OPENCODE_SESSION_ID = await rootSessionID(input.sessionID)
        },
        "tool.execute.before": async (input, output) => {
          if (!workspaceAllowed) {
            throw new Error("Tools are restricted to ${settings.workspacesRoot}")
          }
          ${pkgs.lib.optionalString previewCfg.enable ''
            if (input.tool.startsWith("playwright_")) {
              // MCP connections are project-scoped; route each call using the
              // trusted root conversation, never a model-selected session ID.
              output.args.__opencode_session_id = await rootSessionID(input.sessionID)
            }
          ''}
          const key = tmpPathKeys[input.tool]
          if (key && isTmpPath(output.args?.[key])) {
            throw new Error("Direct file tools cannot access shell temporary files; use bash")
          }
        },
        tool: {
          ${pkgs.lib.optionalString settings.githubBridge.enable ''
            github_track_pr: tool({
              description: "Associate a bot-authored GitHub pull request with the current OpenCode session so future controller feedback resumes this conversation.",
              args: {
                pr_url: tool.schema.string().url().describe("GitHub pull request URL returned by gh pr create"),
              },
              async execute(args, context) {
                await managedRequest("track_pr", { pr_url: args.pr_url }, context)
                return `Registered ${"$"}{args.pr_url} with this conversation`
              },
            }),
            github_manage_remote: tool({
              description: "Safely configure, fetch, or track validated GitHub remotes without exposing arbitrary .git/config writes. Use setup to discover or create a writable bot fork and configure origin/source/upstream.",
              args: {
                action: tool.schema.enum(["setup", "set", "fetch", "track"]),
                repository: tool.schema.string().regex(/^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}\/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/).optional(),
                upstream_repository: tool.schema.string().regex(/^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}\/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/).optional(),
                remote: tool.schema.enum(["origin", "source", "upstream", "all"]).optional(),
                branch: tool.schema.string().max(255).optional(),
              },
              async execute(args, context) {
                if (["setup", "set"].includes(args.action) && !args.repository) {
                  throw new Error(`repository is required for ${"$"}{args.action}`)
                }
                if (args.upstream_repository && args.action !== "setup") {
                  throw new Error("upstream_repository is valid only for setup")
                }
                const result = await managedRequest("manage_github_remote", args, context)
                return JSON.stringify(result, null, 2)
              },
            }),
          ''}
          ci_run: tool({
            description: "Run a GitHub Actions workflow locally with act in the isolated CI account.",
            args: {
              workflow: tool.schema.string().optional().describe("Relative workflow file path"),
              job: tool.schema.string().optional().describe("Workflow job name"),
              event: tool.schema.string().optional().describe("GitHub event name; defaults to push"),
              timeout: tool.schema.string().optional().describe("Maximum duration; defaults to 45m"),
            },
            async execute(args, context) {
              const command = [
                "/run/wrappers/bin/sudo", "-n", "-u", "ci-runner", "--",
                "${agent-ci}/bin/agent-ci", "--source", context.directory,
              ]
              if (args.workflow) command.push("--workflow", args.workflow)
              if (args.job) command.push("--job", args.job)
              if (args.event) command.push("--event", args.event)
              if (args.timeout) command.push("--timeout", args.timeout)

              const child = Bun.spawn(command, { stdout: "pipe", stderr: "pipe" })
              let output = ""
              let truncated = false
              const publish = (text) => {
                output += text
                if (output.length > 100000) {
                  output = output.slice(-100000)
                  truncated = true
                }
                context.metadata({
                  title: "ci_run",
                  metadata: { output: output.slice(-30000) },
                })
              }
              const pump = async (stream) => {
                const decoder = new TextDecoder()
                const reader = stream.getReader()
                while (true) {
                  const next = await reader.read()
                  if (next.done) break
                  publish(decoder.decode(next.value, { stream: true }))
                }
                publish(decoder.decode())
              }
              const abort = () => child.kill()
              context.abort.addEventListener("abort", abort, { once: true })
              const [, , status] = await Promise.all([
                pump(child.stdout),
                pump(child.stderr),
                child.exited,
              ]).finally(() => context.abort.removeEventListener("abort", abort))
              const prefix = truncated ? "[earlier CI output truncated]\n" : ""
              return `${"$"}{prefix}${"$"}{output}\n\nci_run exited with status ${"$"}{status}`
            },
          }),
        },
      }
    }
  '';
in
{
  inherit
    agent-ci
    fish
    github-bridge
    jetls
    jlfmt
    opencode
    opencode-git
    opencode-plugin
    opencode-preview
    opencode-server
    opencode-workspace
    playwright-mcp
    sandbox-exec
    sandbox-runtime
    ;
}
