# OpenCode host environment

This OpenCode server runs as `rnwst-bot` on a NixOS development VPS. Treat
the machine configuration as infrastructure code and keep project work below
`/srv/opencode/workspaces`.

## Working rules

- Work only in the current repository. OpenCode denies built-in tools access
  outside the repository except read-only Nix store paths.
- Every shell tool call starts a new Sandbox Runtime (`srt`) and bubblewrap
  sandbox. It can write only to the current Git worktree and `/tmp`.
- Shell network access is fail-closed and limited to common GitHub, language
  package registry, Nix, and container registry domains. Local port binding,
  SSH, and Unix sockets are blocked.
- OpenCode sharing, snapshots, and automatic self-updates are disabled by
  root-managed configuration. Git is the source of truth for changes.
- Never search for, print, or persist credentials. OpenCode provider auth,
  systemd credentials, and GitHub CLI configuration are outside the sandbox.
  `GH_TOKEN` and Git's HTTP authorization value appear only as sentinels and
  are restored by the network proxy for their GitHub HTTPS destinations.
- All GitHub Git transport uses HTTPS. The operator provisions and maintains
  workspaces with `sudo opencode-git`; normal agent `git` commands authenticate
  through the masked HTTP header without exposing the token.
- Project `opencode.json` files and `.opencode` plugins are disabled. Make
  host-wide OpenCode changes in the Nix-managed configuration, not in a
  repository.

## Available tools

The Nix profile includes Git and GitHub CLI, Helix, Fish, direnv, common Unix
utilities, and development toolchains for Nix, shell, C/C++, Go, Rust, Java,
JavaScript/TypeScript, Python, Julia, JSON, YAML, TOML, XML, HTML, CSS, and
Markdown. Prefer repository-native commands and lock files over global state.

To run CI checks locally, always use the `ci_run` tool for GitHub Actions.
It copies the current worktree into a disposable directory owned by the
credential-free `ci-runner` account, runs `act` through that account's rootless
Docker daemon, streams output, and deletes the copy. The CI account cannot
receive OpenCode, GitHub, SSH, or Cloudflare credentials. Do not invoke Docker
directly from shell commands.

## System changes

The bot has no general sudo access. It can only run the fixed `agent-ci`
command as `ci-runner`. To change installed tools, OpenCode policy, services,
users, storage, or firewall rules:

1. Edit the Nix flake repository that defines this host.
2. Run `nix fmt`, `nix flake check`, and any relevant project checks.
3. Commit the reviewed change.
4. Ask an operator to deploy it with `sudo nixos-rebuild switch --flake .#opencode`.

Do not work around missing packages with changes under `/etc`, system-wide
installers, rootful containers, or privilege escalation. Add the package or
service declaratively to the flake instead.


# Ways of working

## Commit message guidelines

- Adhere to the [conventional commit
guidelines](https://www.conventionalcommits.org/en/v1.0.0/#summary).
- Ensure the commit message body is written in the imperative as well. When
describing previous behaviour, you may use the past tense.  (E.g. "Previously, X
was done. Do Y instead.)
- Ensure that commit messages do not exceed 72 characters line-width. This
should be validated with tooling, rather than counting characters yourself (e.g.
with `awk 'length($0) > 72 { print NR ": " length($0) ": " $0 }' commit.txt`).
The only allowable exception to this are URLs, which may exceed 72 chars.
- Avoid jargon unless absolutely necessary. The commit message should explain
in simple terms what was changed and why. It should provide as much detail as
necessary, but should be concise nonetheless.

## Branch naming convention

When creating branches, adhere to the [conventional branch
guidelines](https://conventionalbranch.org/#summary). Use `feat` instead of
`feature` and `fix` instead of `bugfix`. If you are implementing or fixing an
issue, prefix the issue number, e.g. `feat/4-add-login-page`.
