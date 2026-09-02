{
  pkgs,
  pkgsUnstable,
  settings,
}:
let
  inherit (pkgs) julia;
  inherit (pkgsUnstable) opencode;

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
      exec runuser --user rnwst-bot -- env \
        GIT_TERMINAL_PROMPT=0 \
        HOME=/home/rnwst-bot \
        OPENCODE_GITHUB_TOKEN="$token" \
        XDG_CONFIG_HOME=/home/rnwst-bot/.config \
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
      pkgsUnstable.sandbox-runtime
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
        ${settings.workspacesRoot}/*/) ;;
        *) echo "refusing to run outside ${settings.workspacesRoot}: $root" >&2; exit 77 ;;
      esac

      settings_file="$(mktemp)"
      trap 'rm -f "$settings_file"' EXIT

      if [[ "$mode" == "shell" && -n "''${CREDENTIALS_DIRECTORY:-}" && -r "$CREDENTIALS_DIRECTORY/github-token" ]]; then
        GH_TOKEN="$(<"$CREDENTIALS_DIRECTORY/github-token")"
        GIT_CONFIG_COUNT=3
        GIT_CONFIG_KEY_0=http.https://github.com/.extraHeader
        GIT_CONFIG_VALUE_0="Authorization: Basic $(printf 'x-access-token:%s' "$GH_TOKEN" | base64 --wrap=0)"
        GIT_CONFIG_KEY_1=credential.helper
        GIT_CONFIG_VALUE_1=
        GIT_CONFIG_KEY_2=push.default
        GIT_CONFIG_VALUE_2=current
        export GH_TOKEN
        export GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0
        export GIT_CONFIG_KEY_1 GIT_CONFIG_VALUE_1 GIT_CONFIG_KEY_2 GIT_CONFIG_VALUE_2
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
        --arg opencode_auth "$HOME/.local/share/opencode/auth.json" \
        --arg ssh_dir "$HOME/.ssh" \
        --arg credentials_dir "''${CREDENTIALS_DIRECTORY:-/run/credentials}" \
        --arg bwrap "${pkgs.bubblewrap}/bin/bwrap" \
        --arg rg "${pkgs.ripgrep}/bin/rg" \
        --arg socat "${pkgs.socat}/bin/socat" \
        '{
          filesystem: {
            denyRead: [$home, $opencode_auth, $ssh_dir, $credentials_dir],
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
            strictAllowlist: true,
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
          ripgrep: { command: $rg },
          git: { safeDirectories: [$root] }
        }' > "$settings_file"

      ${pkgsUnstable.sandbox-runtime}/bin/srt --settings "$settings_file" -- "$@"
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
    import { realpathSync } from "node:fs"

    const { tool } = await import(
      Bun.resolveSync("@opencode-ai/plugin", "/home/rnwst-bot/.config/opencode"),
    )

    export const ManagedHost = async ({ directory }) => {
      let workspaceAllowed = false
      try {
        const root = realpathSync("${settings.workspacesRoot}")
        const current = realpathSync(directory)
        workspaceAllowed = current === root || current.startsWith(root + "/")
      } catch {}

      return {
        "tool.execute.before": async () => {
          if (!workspaceAllowed) {
            throw new Error("Tools are restricted to ${settings.workspacesRoot}")
          }
        },
        tool: {
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
              const publish = (text) => {
                output += text
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
              return `${"$"}{output}\n\nci_run exited with status ${"$"}{status}`
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
    jetls
    jlfmt
    opencode
    opencode-git
    opencode-plugin
    opencode-server
    sandbox-exec
    ;
}
