# OpenCode NixOS VPS

This repository installs a single-purpose autonomous development host on an
OVHcloud VPS. NixOS, OpenCode, the development toolchains, the Cloudflare
Tunnel, command sandbox, and isolated local CI runner are all declared by one
flake.

> [!WARNING]
> `nixos-anywhere` and Disko erase the configured disk. Verify the target host
> and stable disk identifier immediately before running the install command.
> There is no confirmation prompt in this configuration.

## Objectives

- Use stable NixOS 26.05 as the base and pin every flake input in `flake.lock`.
- Use unstable nixpkgs only for Fish 4.8, OpenCode, and Sandbox Runtime.
- Expose one password-protected OpenCode server through Cloudflare Tunnel at
  `https://opencode.<MY-DOMAIN>`, with no public origin port or nginx.
- Give the agent broad development tooling without general sudo access.
- Treat prompts and repository content as untrusted; enforce security with
  operating-system and account boundaries rather than model compliance.
- Put each agent shell command in a fresh, fail-closed Sandbox Runtime and
  bubblewrap sandbox.
- Prevent agent access to host credentials, paths outside its worktree,
  privileged Docker sockets, SSH, and persistent project-supplied plugins.
- Run GitHub Actions in disposable copies under a credential-free account and
  rootless Docker, never the OpenCode account or rootful Docker.
- Route controller-authorized GitHub assignments, reviews, and mentions into
  persistent OpenCode sessions without public webhook infrastructure.
- Keep credentials and installation-specific values out of the Nix store and
  Git history.

## Architecture

| Component      | Design                                                                                     |
| -------------- | ------------------------------------------------------------------------------------------ |
| Base system    | NixOS 26.05, x86-64, GPT with BIOS and EFI boot, compressed Btrfs root, 25% zram           |
| Public ingress | Cloudflare Tunnel to OpenCode on `127.0.0.1:4096`                                          |
| Host firewall  | TCP 22 only; port 4096 is loopback-only                                                    |
| `rnwst-admin`  | Primary operator with passwordless sudo and the full development profile                   |
| `rnwst-bot`    | OpenCode and workspace owner, SSH enabled, no general sudo                                 |
| `ci-runner`    | No credentials or login shell; rootless Docker and disposable `act` jobs                   |
| OpenCode       | Pinned package, HTTP Basic Auth, OpenAI provider, sharing/snapshots/autoupdate disabled    |
| Agent commands | Managed shell wrapper, Sandbox Runtime, bubblewrap, curated egress, worktree-only writes   |
| GitHub         | Root-owned token for operators; masked API and Git HTTPS authentication for agents         |
| GitHub bridge  | Conditional notification polling, numeric controller-ID checks, persistent session routing |
| Task storage   | Canonical Btrfs repositories with cheap, independent writable task snapshots                |
| Configuration  | Managed `/etc/opencode/opencode.json` and `/etc/opencode/AGENTS.md`                        |

OpenCode project configuration and project plugins are disabled. This prevents
an untrusted checkout from loading code in the long-running OpenCode process or
overriding managed permissions. Repository `AGENTS.md` instructions still
describe project-specific work, but host policy comes from
`config/AGENTS.md`. A root-managed hook resolves each selected project path and
rejects all tool calls outside `/srv/opencode/workspaces`, including workspace
symlink escapes.

The command sandbox denies bot-home reads except Git configuration. It denies
writes outside the current Git worktree and a session-private `/tmp`, where
common language and package-manager caches are redirected. Temporary data is
backed by `/srv/opencode/workspace-tmp/<WORKSPACE>/<SESSION_ID>` (with `.tasks`
for automated workspaces), hidden from other sessions, and deleted with the
session or workspace. It permits general outbound access while blocking local
binding, Unix sockets, and TCP port 22. The real GitHub token and a precomputed
HTTP Basic credential are replaced by independent sentinels inside the sandbox.
Git receives the masked credential through environment-based configuration, and
Sandbox Runtime's TLS proxy restores it only in HTTPS requests to `github.com`.
GitHub API clients use the separately masked token, restored for `github.com`
and `api.github.com`.

These controls contain common prompt-injection outcomes such as credential
theft, workspace escape, host persistence, sudo use, and Docker-socket escape.
They do not stop an agent from changing workspace code, using allowed GitHub
operations, or disclosing workspace content through permitted tools. Never put
secrets in a workspace. LSPs, formatters, and built-in web tools are outside
the shell sandbox.

Workspaces live in `/srv/opencode/workspaces` rather than the bot's home so
project data is separate from private authentication, configuration, caches,
and shell startup files. This lets the sandbox deny bot-home access and lets
`ci-runner` read source through the `agent-workspaces` group without opening
`/home/rnwst-bot`. It also leaves the workspace tree easy to mount, back up,
or quota independently later.

Named ACLs give `rnwst-admin` and `rnwst-bot` read/write access to workspaces,
while the owning `agent-workspaces` group keeps `ci-runner` read-only. Admin
also has read/write ACL access to the bot home for direct maintenance. This
includes OpenCode state and ChatGPT OAuth credentials, so processes running as
admin must be treated as fully trusted; admin already has passwordless sudo.

`act` is not a perfect reproduction of GitHub-hosted Actions. CI containers
share the VPS kernel and can consume host resources, but they run as
`ci-runner`, receive no OpenCode, GitHub, SSH, or Cloudflare credentials, and
operate on a disposable copy rather than the source worktree. `ci-runner`
uses only its own rootless Docker socket; rootful Docker is disabled, and agent
shell commands cannot create Unix sockets.

Canonical repositories live outside the workspace tree under
`/var/lib/opencode-task-bases`. Every automated GitHub task receives a writable
Btrfs snapshot under `/srv/opencode/workspaces/.tasks`; unchanged repository
history and working files share disk extents. Canonical repositories are fetched
under a per-repository lock before every snapshot. Manual workspaces remain
directly below `/srv/opencode/workspaces` and are never collected automatically.
Automated names remain unique while identifying their GitHub subject, for
example `task-rnwst-fish-helix-pr-6-5eeebd5902cc`. OpenCode clients use this
directory basename as the workspace label.

## Repository Layout

| Path                                        | Purpose                                                          |
| ------------------------------------------- | ---------------------------------------------------------------- |
| `flake.nix`                                 | Inputs, NixOS configuration, packages, checks, and dev shell     |
| `hosts/opencode/settings.nix`               | Public installation-specific settings                            |
| `hosts/opencode/default.nix`                | Host, users, SSH, firewall, boot, and Home Manager               |
| `hosts/opencode/disko.nix`                  | Destructive disk partitioning and filesystems                    |
| `hosts/opencode/hardware-configuration.nix` | Hardware defaults, replaced during deployment                    |
| `hosts/opencode/development-home.nix`       | Shared development tools, Fish-Helix, Helix, direnv, and prompt   |
| `hosts/opencode/home.nix`                   | Bot-specific Git identity and HTTPS credential helper            |
| `hosts/opencode/admin-home.nix`             | Administrator identity plus `og` and `oca` operator helpers       |
| `modules/opencode.nix`                      | Managed OpenCode, systemd credentials, and Cloudflare Tunnel     |
| `modules/github-bridge.nix`                 | GitHub polling, credentials, queue activation, and service policy |
| `modules/opencode-workspaces.nix`           | Btrfs workspace storage and operator tooling                     |
| `modules/ci-runner.nix`                     | Rootless Docker and restricted sudo rule                         |
| `pkgs/default.nix`                          | Sandbox, operator Git, server, CI, and development wrappers       |
| `pkgs/github-bridge/`                       | GitHub event routing, SQLite state, and OpenCode API client       |
| `pkgs/opencode-workspace/`                  | Canonical repositories and Btrfs workspace manager               |
| `tests/github-bridge/`                      | Side-effect-free bridge unit and fake-HTTP tests                  |
| `tests/nixos/github-bridge.nix`             | Real OpenCode and Btrfs NixOS VM test                             |
| `config/AGENTS.md`                          | Nix-managed host instructions presented to OpenCode              |

## Prerequisites

- A trusted operator workstation. The default commands below assume NixOS;
  non-NixOS setup is documented next.
- An OVHcloud VPS running Linux and reachable as `root` over SSH.
- A domain managed through Cloudflare.
- A GitHub account or bot account that can access the selected repositories.
- Backups of anything currently stored on the target disk.

### NixOS Operator Workstation

Enable the required commands declaratively, then rebuild the workstation:

```nix
nix.settings.experimental-features = [ "nix-command" "flakes" ];
```

The unqualified `nix fmt`, `nix flake check`, and `nix run` commands below then
use the normal daemon-backed `/nix/store`.

### Non-NixOS Operator Workstation

On Arch, install Nix and initialize the multi-user store before starting its
daemon:

```bash
sudo pacman -S nix

sudo systemctl stop nix-daemon.service
sudo install -d -o root -g root -m 0755 \
  /nix /nix/var /nix/var/log /nix/var/log/nix /nix/var/log/nix/drvs \
  /nix/var/nix /nix/var/nix/db /nix/var/nix/gcroots \
  /nix/var/nix/gcroots/per-user /nix/var/nix/profiles \
  /nix/var/nix/profiles/per-user /nix/var/nix/temproots \
  /nix/var/nix/userpool /nix/var/nix/daemon-socket
sudo install -d -o root -g nixbld -m 1775 /nix/store
sudo systemctl enable --now nix-daemon.service

nix --extra-experimental-features nix-command \
  --store daemon \
  store ping
df -h /nix/store
```

Keep at least 15 GiB free on the filesystem containing `/nix/store`; this
configuration includes several large development toolchains.

Use `--store daemon` on each daemon-backed command. Alternatively, set
`NIX_REMOTE=daemon` in the shell and omit that option:

```text
# Fish
set -gx NIX_REMOTE daemon

# Bash and other POSIX shells
export NIX_REMOTE=daemon
```

On other distributions, use an equivalent multi-user, daemon-backed Nix
installation. Section 5 provides commands for both daemon-backed and local
stores.

## 1. Configure Public Values

Edit `hosts/opencode/settings.nix`.

The template intentionally fails evaluation while `operatorKeys` is empty, so
it cannot install a system with no administrative login path.

Set `diskDevice` only after the target preflight. Set `cloudflareTunnelId` to
the tunnel UUID after creating the tunnel. A `null` tunnel ID intentionally
disables cloudflared while leaving OpenCode available through an SSH port
forward.

Review the hostname, public hostname, operator SSH keys, workspace root, and
secret paths. Replace every template value before deployment. These identifiers
are not credentials, but they can identify a live host; do not commit them if
that association should remain private.

`githubBridge` selects one agent and model for all GitHub-triggered work. The
committed configuration uses `dryRun = false`, so newly discovered commands are
processed after the initial notification baseline. Set it to `true` before
deployment when proposed actions should be reviewed first. The initial defaults
allow four concurrent automated tasks, require 15 percent free space, and retain
completed task snapshots for 30 days.

To list models available to the bot account, run:

```bash
sudo -iu rnwst-bot opencode models
```

Each result has the form `<PROVIDER_ID>/<MODEL_ID>`. Set the corresponding
values in `githubBridge.model`:

```nix
model = {
  providerID = "openai";
  modelID = "gpt-5.6-sol";
};
```

Rebuild the host and run the bridge once after changing the model:

```bash
sudo nixos-rebuild switch --flake .#opencode
sudo systemctl start github-bridge.service
```

The bridge validates that exact provider/model pair before allocating a task
workspace, so an unavailable model leaves the GitHub event pending rather than
silently using a fallback.

## 2. Create the Cloudflare Tunnel

Run this on the trusted workstation with `cloudflared` installed:

```bash
cloudflared tunnel login
cloudflared tunnel create opencode
cloudflared tunnel route dns <TUNNEL_UUID> opencode.<MY-DOMAIN>
```

Copy the UUID into `settings.cloudflareTunnelId`. The create command writes
`~/.cloudflared/<TUNNEL_UUID>.json`; that JSON file is the tunnel credential
installed later. `cert.pem` is not needed on the VPS to run an existing
tunnel.

The DNS route can instead be created in the Cloudflare dashboard. Cloudflare
Access can be added as another authentication layer, but OpenCode HTTP Basic
Auth remains mandatory in this configuration.

## 3. Prepare Secrets

`bootstrap-secrets/` is ignored by Git. Create the directory, server password,
and tunnel credential on a trusted machine:

```bash
umask 077
mkdir -p bootstrap-secrets
openssl rand -base64 32 > bootstrap-secrets/server-password
install -m 0600 ~/.cloudflared/<TUNNEL_UUID>.json \
  bootstrap-secrets/cloudflared.json
```

### GitHub HTTPS Token

Create a classic personal access token in GitHub under **Settings > Developer
settings > Personal access tokens > Tokens (classic) > Generate new token
(classic)**. A classic token is required when the agent must push to a fork and
open pull requests against repositories owned by someone else; fine-grained
tokens are restricted to repositories under one selected resource owner.
Configure the classic token as follows:

- Use a descriptive name such as `opencode-vps` and a practical expiration,
  such as 30 or 90 days.
- Grant `public_repo` for public repositories, or `repo` only when private
  repository access is required.
- Grant `notifications` so the bridge can discover assignments, review
  requests, and mentions without changing notification read state.
- Grant `workflow` only if the agent must add or modify files under
  `.github/workflows`.

Do not grant `admin:org`, `admin:public_key`, `admin:repo_hook`, `delete_repo`,
`gist`, package, or `user` scopes unless a specific task requires them. Use a
GitHub App instead of a classic PAT for a long-lived or centrally managed
multi-organization integration.

Create the token file without putting the token in shell history, then paste
only the token into the editor:

```bash
install -m 0600 /dev/null bootstrap-secrets/github-token
$EDITOR bootstrap-secrets/github-token

# Resolve the immutable numeric ID; do not use the account's login name here.
gh api users/<CONTROLLER_LOGIN> --jq .id \
  > bootstrap-secrets/github-controller-id

# The public user endpoint also works without GitHub CLI authentication.
curl --fail --silent --show-error \
  -H 'Accept: application/vnd.github+json' \
  -H 'User-Agent: opencode-vps-bootstrap' \
  https://api.github.com/users/<CONTROLLER_LOGIN> \
  | jq -er '.id | numbers' \
  > bootstrap-secrets/github-controller-id

chmod 0600 bootstrap-secrets/github-controller-id
```

The controller ID is not secret, but keeping it in a runtime file avoids tying
the public template to one GitHub identity. The bridge authorizes commands by
this immutable numeric ID, never by login name or notification reason.

The token is exposed to agent commands only as a Sandbox Runtime sentinel and
is restored only for GitHub HTTPS requests. CI jobs never receive it. Record
its expiration (if applicable) and rotate it before that date. OpenCode receives
the token through systemd `LoadCredential`, so restart `opencode.service` after
installing or replacing the token file.

Do not use `builtins.readFile`, `environment.etc.*.text`, or a flake input for
secrets. Any value evaluated by Nix can become world-readable in `/nix/store`.

## 4. Identify the Target Disk

`nixos-anywhere` requires key-based root SSH. If the provider image initially
allows only a sudo-capable user, install the operator public key in
`/root/.ssh/authorized_keys` and temporarily set `PermitRootLogin
prohibit-password` in an SSH server drop-in. Keep password and keyboard-
interactive authentication disabled, validate the configuration with `sshd
-t`, reload SSH, and verify root login from a second terminal before continuing.
The temporary image configuration is replaced by the NixOS installation,
which disables root SSH again.

`nixos-anywhere` starts from the VPS's existing Linux installation, kexecs a
temporary NixOS installer, runs Disko, and installs the configured system.
Rescue mode is not required for the normal installation path.

The target is the OVH VPS's whole virtual system disk, such as `/dev/sda` or
`/dev/vda`, not a partition like `/dev/sda1` or a separate installer disk.

Inspect the target through the existing system:

```bash
ssh root@<VPS_IP> 'lsblk -e7 -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL,SERIAL'
ssh root@<VPS_IP> 'ls -l /dev/disk/by-id/'
```

Prefer a unique `/dev/disk/by-id/...` link confirmed with both `readlink -f`
and `lsblk`; never infer the disk from a device letter copied from the OVH
dashboard. The configured path must remain available after
`nixos-anywhere` kexecs its installer.

If the system exposes no stable by-id link, stop and verify the device by size,
model, serial, and current partitions. A temporary `/dev/sdX` value is a last
resort for that one installation and must be rechecked immediately before
execution.

Update `settings.diskDevice`, then verify the exact path remotely:

```bash
ssh root@<VPS_IP> 'readlink -f /dev/disk/by-id/<CONFIRMED_DISK_ID>'
```

Use OVH rescue mode only as a fallback when the existing system is unbootable,
does not permit root SSH, or cannot kexec successfully. If rescue is used,
repeat the complete disk preflight there before installing: OVH rescue may
enumerate the same disk under a different device name or expose different
by-id links.

## 5. Evaluate and Install

Format and evaluate before touching the VPS:

On NixOS:

```bash
nix fmt
nix flake check
```

On a daemon-backed non-NixOS system (required before deployment):

```bash
nix --extra-experimental-features 'nix-command flakes' \
  --store daemon \
  fmt
nix --extra-experimental-features 'nix-command flakes' \
  --store daemon \
  flake check
```

For optional validation on a non-NixOS system without a working `/nix/store`,
use the ignored repository-local store:

```bash
nix --extra-experimental-features 'nix-command flakes' fmt \
  --store "local?root=$PWD/.nix-local"
nix --extra-experimental-features 'nix-command flakes' flake check \
  --store "local?root=$PWD/.nix-local"
```

A local-store check does not populate the daemon store. Before deployment from
non-NixOS, the daemon-backed `nix flake check` above must complete; this builds
the closure and catches storage failures before `nixos-anywhere` kexecs the
VPS.

Install from the repository root. The hardware generation option updates
`hosts/opencode/hardware-configuration.nix` with the target scan:

```bash
nix run github:nix-community/nixos-anywhere -- \
  --flake .#opencode \
  --generate-hardware-config nixos-generate-config \
  ./hosts/opencode/hardware-configuration.nix \
  root@<VPS_IP>
```

On a daemon-backed non-NixOS workstation, run:

```bash
nix --extra-experimental-features 'nix-command flakes' \
  --store daemon \
  run \
  github:nix-community/nixos-anywhere -- \
  --flake .#opencode \
  --generate-hardware-config nixos-generate-config \
  ./hosts/opencode/hardware-configuration.nix \
  root@<VPS_IP>
```

Do not use the repository-local store to launch `nixos-anywhere` on Arch. Use
the daemon-backed command above so `nixos-anywhere` can prefer Arch's system
SSH client. The local store remains suitable for `nix fmt` and
`nix flake check`; no changes to Arch's package-owned SSH configuration are
required.

This command kexecs the installer, partitions the configured disk, installs
NixOS, and normally reboots the VPS. If the rescue fallback was used, switch
the OVH boot mode back to local disk. The new root account has no SSH login;
connect with the configured operator key:

```bash
ssh-keygen -R <VPS_IP>
ssh rnwst-admin@<VPS_IP>
```

Inspect and commit the generated hardware configuration after a successful
installation.

## 6. Install Runtime Secrets

The first boot is intentionally usable over SSH even though OpenCode, the
GitHub bridge, and the tunnel fail closed until their credential files exist.

Copy the prepared files to the administrator's home, install them with root
ownership, then remove the transfer copies:

```bash
scp bootstrap-secrets/server-password \
  bootstrap-secrets/github-token \
  bootstrap-secrets/github-controller-id \
  bootstrap-secrets/cloudflared.json \
  rnwst-admin@<VPS_IP>:/tmp/
```

```bash
ssh rnwst-admin@<VPS_IP>
sudo install -d -m 0700 -o root -g root /var/lib/opencode-secrets
sudo install -m 0400 -o root -g root /tmp/server-password /var/lib/opencode-secrets/server-password
sudo install -m 0400 -o root -g root /tmp/github-token /var/lib/opencode-secrets/github-token
sudo install -m 0400 -o root -g root /tmp/github-controller-id /var/lib/opencode-secrets/github-controller-id
sudo install -m 0400 -o root -g root /tmp/cloudflared.json /var/lib/opencode-secrets/cloudflared.json
rm -f /tmp/server-password /tmp/github-token /tmp/github-controller-id /tmp/cloudflared.json
sudo systemctl restart opencode.service
sudo systemctl restart github-bridge.timer
sudo systemctl restart cloudflared-tunnel-<TUNNEL_UUID>.service
```

Omit the cloudflared file and restart when `cloudflareTunnelId = null`.

When upgrading a host that used the earlier GitHub SSH-key design, remove the
retired key files after deploying this revision:

```bash
sudo rm -f \
  /var/lib/opencode-secrets/github-bot-ed25519 \
  /home/rnwst-bot/.ssh/id_ed25519_github
```

On the operator workstation:

```bash
rm -f bootstrap-secrets/github-bot-ed25519*
```

Also delete the corresponding account SSH key or repository deploy key in
GitHub. Do not remove the operator key from `settings.operatorKeys`; it is used
to log in to the VPS.

## 7. Connect ChatGPT Pro

OpenCode stores provider OAuth state in
`/home/rnwst-bot/.local/share/opencode/auth.json`. It is not Nix-managed and
the command sandbox cannot read it.

Use OpenCode's headless device flow; it does not require port forwarding.
Connect to the VPS normally, stop the server, and run login from the bot's
login environment:

```bash
ssh rnwst-admin@<VPS_IP>
```

On the VPS:

```bash
sudo systemctl stop opencode
sudo -iu rnwst-bot opencode auth login \
  --provider openai \
  --method 'ChatGPT Pro/Plus (headless)'
sudo systemctl start opencode
```

Open the printed device URL in any browser, enter the displayed code, and wait
for the CLI to confirm authentication. `sudo -iu` changes the user, home, and
working directory, preventing OpenCode from trying to read configuration from
the administrator's home. Check service logs if the server cannot read the
resulting authentication state:

```bash
sudo journalctl -u opencode -n 100 --no-pager
```

## 8. Verify the Installation

Check the local server without exposing the password on a command line:

```bash
sudo bash -c '
  password=$(< /var/lib/opencode-secrets/server-password)
  curl --fail --user "opencode:$password" http://127.0.0.1:4096/global/health
'
```

Check services and listeners:

```bash
systemctl status opencode
systemctl status cloudflared-tunnel-<TUNNEL_UUID>
sudo -u ci-runner env XDG_RUNTIME_DIR=/run/user/$(id -u ci-runner) systemctl --user status docker
sudo ss -ltnp
```

Only SSH should listen on a public address. OpenCode must listen on
`127.0.0.1:4096`. Visit `https://opencode.<MY-DOMAIN>` and enter username
`opencode` with the generated server password.

Verify the operator Git wrapper against one repository selected for the token:

```bash
sudo opencode-git ls-remote \
  https://github.com/<OWNER>/<REPOSITORY>.git HEAD
```

`opencode-git` starts in `/srv/opencode/workspaces`, runs Git as `rnwst-bot`,
and supplies the root-owned token through an ephemeral HTTPS credential helper.
It never stores the token in Git configuration or a remote URL. In the admin
Fish shell, `og` abbreviates `sudo opencode-git`; normal `git` remains unchanged.

Verify the token without placing it in shell history or process arguments:

```bash
sudo systemd-run --wait --pipe --collect \
  --uid=rnwst-bot \
  --gid=agent-workspaces \
  --property=LoadCredential=github-token:/var/lib/opencode-secrets/github-token \
  /run/current-system/sw/bin/bash -c '
    GH_TOKEN=$(< "$CREDENTIALS_DIRECTORY/github-token")
    export GH_TOKEN
    /etc/profiles/per-user/rnwst-bot/bin/gh auth status
    /etc/profiles/per-user/rnwst-bot/bin/gh api user --jq .login
  '
```

In OpenCode, ask the agent to run `gh auth status` and `gh api user --jq
.login`. Both run in Sandbox Runtime with the masked token.

Ask the agent to push a disposable test branch with normal `git push`. Confirm
that the branch appears on the configured GitHub remote while printing
`GH_TOKEN` or Git's authorization configuration reveals only sentinel values.

Test `ci_run` on a small repository with a known workflow. Confirm its output
streams in the tool UI, the source worktree remains unchanged, and no
`/var/lib/ci-runner/jobs/job.*` directory remains afterward.

## GitHub Bridge

The bridge polls GitHub Notifications because repository webhooks require admin
access and GitHub App webhooks only cover repositories where the App is
installed. Notifications are discovery hints only. Before acting, the bridge
fetches the underlying event and verifies its actor, assigner, review requester,
or author against the numeric controller ID. It does not mark notifications
read.

GitHub may publish a notification shortly after its underlying comment or review.
After the initial hard baseline, the bridge allows up to five minutes of bounded
notification propagation delay so fresh commands are not mistaken for historical
activity. Older previously unseen events remain baselined.

The bridge accepts one command anywhere in a controller-authored comment. Text
before the mention is not part of the instruction; the action and all following
text are. Additional instruction text is optional, and multiple commands in one
comment are rejected as ambiguous:

```text
@rnwst-bot answer [additional instruction]
@rnwst-bot implement [additional instruction]
@rnwst-bot review [additional instruction]
@rnwst-bot continue [additional instruction]
@rnwst-bot cancel
```

| Command     | Issue                             | Pull request                                            | Discussion                               |
| ----------- | --------------------------------- | ------------------------------------------------------- | ---------------------------------------- |
| `answer`    | Answer the issue question         | Post an explanatory PR comment                          | Answer the Discussion                    |
| `implement` | Implement the issue and open a PR | Implement requested PR changes or create a follow-up PR | Implement the proposal and open a PR     |
| `review`    | Review the proposal and comment   | Review code and submit a `COMMENT` review               | Review the proposal and answer           |
| `continue`  | Resume the active mapped session  | Resume the active implementation session                | Resume the active answer or task session |
| `cancel`    | Cancel queued and ongoing work    | Cancel queued and ongoing work                          | Cancel queued and ongoing work           |

| Automatic trigger                    | Verification                                                              | Action      |
| ------------------------------------ | ------------------------------------------------------------------------- | ----------- |
| Bot assigned to an issue             | Controller performed the assignment and the bot remains assigned         | `implement` |
| Bot requested as a PR reviewer       | Controller requested the review and the request remains active           | `review`    |
| Controller submits a tracked PR review | Review author matches the controller; pending reviews remain ignored    | `continue`  |

`answer`, `implement`, and `review` always create a new task snapshot and
OpenCode session. The new session becomes the active mapping for that GitHub
subject without deleting older sessions. `continue` resumes the active mapping;
`cancel` aborts current work and pauses that mapping, and a later `continue`
resumes it. A session may be linked explicitly to several subjects, such as an
issue and its implementation PR. Either linked subject can continue that same
session. Mappings are never inferred merely because subjects share a repository.
If trusted review feedback arrives for a bot-authored PR before registration,
the bridge waits five minutes for the original session to register. If no
mapping appears, it creates a replacement implementation session so feedback is
not stranded after state loss or an interrupted registration.

After creating a PR, the agent calls `github_track_pr` with the returned URL.
The tool obtains the current session and directory from OpenCode, verifies that
the bot authored the PR, and makes that session active for future PR feedback.
The `github_manage_remote` tool sends similarly authenticated requests for
validated GitHub repository setup. It can create or discover the bot fork,
manage only `origin`, `source`, and `upstream`, fetch branch refs, and persist
tracking for a validated local branch; it cannot write arbitrary Git settings.

Initial bridge prompts contain only the subject URL, title, body, prepared
checkout, and verified controller instruction. PR reviews do not include a full
diff; issue comments, Discussion replies, other users' reviews, and complete CI
logs are not copied by default. Agents inspect local diffs and use targeted `gh`
or GraphQL requests when the controller references additional context.

Comments and submitted reviews use their stable GitHub object ID for
deduplication. Immediately before every new-session or continuation prompt, the
bridge re-fetches that exact object, revalidates its controller, updates SQLite,
and formats the latest content as Markdown sections and bullets. If the command,
PR head, or authorization changed while a workspace was being prepared, the
bridge safely retries or discards the pending action. Once the deterministic
OpenCode message exists, processing has started and later edits are intentionally
ignored; submit a new comment or review to provide additional instructions.

The committed configuration starts the bridge in live mode. For a dry-run
rollout, set `githubBridge.dryRun = true` and rebuild before allowing the bridge
to poll. Dry-run mode still polls notifications, re-fetches the underlying
GitHub objects, verifies the controller's numeric user ID, records state, and
logs authorized proposed actions. It does not create task workspaces, create or
prompt OpenCode sessions, execute commands, or collect old tasks. The initial
poll baselines existing notifications, and events observed as dry-run are not
replayed after activation; submit a new command after enabling the bridge when
an action should run. Inspect proposed actions and local state:

```bash
sudo systemctl start github-bridge.service
sudo journalctl -u github-bridge --no-pager
sudo github-bridge status
```

After reviewing dry-run behavior, set `githubBridge.dryRun = false`, rebuild,
and start the service again. The persisted baseline prevents existing unread
notifications from unexpectedly starting historical work. When deploying the
committed live default directly, verify that the first baseline poll completes
before creating a new actionable comment, assignment, or review request.

No separate automation reacts to failed checks or merge conflicts. Agents run
`ci_run` before finishing; later remote-only failures or conflicts can be sent
back to the mapped session explicitly:

```text
@rnwst-bot continue Fix the failing remote checks.
@rnwst-bot continue Rebase this branch and resolve the conflicts.
```

## Clients And Sessions

### TUI On The VPS

Attach the TUI to a workspace, then use `/sessions` to select an existing
session:

```fish
oca <REPOSITORY>
```

`oca` securely reads the server password for the attach process and connects
to the existing server. Plain `opencode` would start a separate instance. To
open a known session directly, run `oca <REPOSITORY> --session <SESSION_ID>`.

### OpenCode Mobile

[OpenCode Mobile](https://github.com/alvarolorentedev/opencode-mobile) is a
community Android client. Configure it with:

```text
Server URL: https://opencode.<MY-DOMAIN>
Username:   opencode
Password:   <server-password>
```

Open **Workspace**, press **Sync projects**, select the repository, then choose
or create a session. If a repository is missing, first open it and create a
session in the browser so the server catalogs it.

Use the app instead of the browser for text-to-speech, voice input and
conversation mode, completion notifications, background checks, native
attachments, and mobile permission handling. Otherwise the browser is simpler.
The app stores connection credentials in Android `AsyncStorage`, so treat it as
a privileged third-party client.

## Daily Operation

Create a manual workspace from an existing GitHub repository, then attach
OpenCode:

```fish
ocw create <OWNER>/<REPOSITORY> [WORKSPACE_NAME]
oca <WORKSPACE_NAME>
```

`ocw` abbreviates `sudo opencode-workspace`. The manager fetches the canonical
repository unconditionally and creates a writable Btrfs snapshot. Manual
workspaces are not subject to automated task cleanup.

Start without a remote and attach or publish one later:

```fish
ocw init my-project
oca my-project

ocw set-remote my-project <OWNER>/<EXISTING_REPOSITORY>
ocw publish my-project <OWNER>/<NEW_REPOSITORY>
ocw publish internal-project <ORGANIZATION>/<NEW_REPOSITORY> --visibility private
```

`publish` defaults to public visibility. It creates the GitHub repository, adds
the HTTPS origin, and pushes the current branch when commits exist. Other
workspace-manager commands are:

```fish
ocw list
ocw refresh <OWNER>/<REPOSITORY>
ocw remove <WORKSPACE_NAME>
ocw remove <WORKSPACE_NAME> --force
```

By default, removal refuses dirty workspaces and explains that it is fetching
into a temporary bare repository to verify every commit remains recoverable from
a configured remote; it never updates the workspace being deleted. `--force`
skips both cleanliness and remote-reachability checks, immediately deleting the
validated manual Git workspace even when it contains uncommitted changes or
unpushed commits. Admin can inspect and edit workspace files directly with the
shared development profile.
Use normal `git` for local operations; agent GitHub operations use the masked
HTTPS credential, while administrators may use `og` when explicit operator
credentials are required. Open or select the workspace in the OpenCode web UI,
then sync projects in mobile clients.

Useful operator commands:

```bash
sudo systemctl status opencode
sudo systemctl status github-bridge.timer
sudo journalctl -fu opencode
sudo journalctl -fu github-bridge
sudo journalctl -fu cloudflared-tunnel-<TUNNEL_UUID>
sudo github-bridge status --blocked
sudo -u ci-runner env XDG_RUNTIME_DIR=/run/user/$(id -u ci-runner) systemctl --user status docker
```

Completed automated task snapshots are collected after 30 days only when the
worktree is clean, commits are reachable from a remote, the session is idle,
and no feedback remains queued. Dirty or unpushed tasks remain present and
appear in `github-bridge status --blocked`. OpenCode sessions and bridge records
are retained indefinitely, so a collected task can be recreated at its original
path when late feedback arrives.

Review old sessions before coordinated deletion:

```bash
sudo github-bridge sessions --older-than 1y
sudo github-bridge delete-session <SESSION_ID>
sudo github-bridge delete-sessions --older-than 1y --confirm
```

These commands remove the OpenCode session and associated automated task state;
they never remove manual workspaces.

Deploy reviewed configuration changes from a checkout on the VPS:

```bash
sudo nixos-rebuild switch --flake .#opencode
```

From a non-NixOS workstation, stage the secret-free checkout and rebuild as
root on the VPS:

```bash
ssh rnwst-admin@<VPS_IP> \
  'rm -rf /tmp/rnwst-bot && mkdir -m 0700 /tmp/rnwst-bot'
rsync --archive \
  .gitignore README.md flake.lock flake.nix config hosts modules pkgs \
  rnwst-admin@<VPS_IP>:/tmp/rnwst-bot/
ssh rnwst-admin@<VPS_IP> \
  'sudo nixos-rebuild switch --flake /tmp/rnwst-bot#opencode'
```

After first deploying the shared-access ACLs, migrate existing files and add
inherited defaults to existing directories:

```bash
sudo setfacl -R -m \
  'u:rnwst-admin:rwX,u:rnwst-bot:rwX,g::r-X,m::rwx,o::---' \
  /srv/opencode/workspaces
sudo find /srv/opencode/workspaces -type d -exec setfacl -m \
  'd:u::rwx,d:u:rnwst-admin:rwx,d:u:rnwst-bot:rwx,d:g::r-x,d:m::rwx,d:o::---' \
  {} +

sudo setfacl -R -m \
  'u:rnwst-admin:rwX,u:rnwst-bot:rwX,m::rwx' \
  /home/rnwst-bot
sudo find /home/rnwst-bot -type d -exec setfacl -m \
  'd:u::rwx,d:u:rnwst-admin:rwx,d:u:rnwst-bot:rwx,d:g::---,d:m::rwx,d:o::---' \
  {} +
```

Verify effective access with `getfacl /srv/opencode/workspaces` and
`getfacl /home/rnwst-bot`. Mode bits shown by `ls` represent the ACL mask and
do not by themselves show each user's effective permissions.

The administrator is intentionally not a trusted Nix user. Building on the
VPS under sudo avoids copying unsigned local derivations into its protected
store while preserving that boundary.

Update pins deliberately. Review OpenCode and Sandbox Runtime release changes
before deploying because both define security-sensitive behavior:

```bash
nix flake update
nix fmt
nix flake check
```

Nix garbage collection runs weekly and removes generations older than 14
days. Rootless Docker data is separate; inspect and prune it explicitly as
`ci-runner` when disk usage requires it.

## Recovery

If `nixos-anywhere` fails after kexec, use its log to determine whether Disko
started. Before `Formatting hard drive with disko`, the VPS disk is untouched:
reboot it from local disk in the OVH panel. Once Disko starts, assume the disk
was modified and boot OVH rescue mode. A failed run may leave a temporary
installer whose generated SSH key is no longer available; do not guess a
password.

If OpenCode does not start, inspect `journalctl -u opencode`. Missing
`server-password` or `github-token` credentials are expected to stop the unit.
If the bridge fails, also verify `github-controller-id`. Reinstall the file with
mode `0400`, root ownership, and restart the corresponding unit.

If the tunnel does not start, verify that the UUID in settings matches the
credential JSON and inspect the corresponding cloudflared unit. Set
`cloudflareTunnelId = null`, rebuild, and use an SSH forward while repairing
Cloudflare:

```bash
ssh -L 4096:127.0.0.1:4096 rnwst-admin@<VPS_IP>
```

Then browse to `http://127.0.0.1:4096` through the tunnel.

Roll back a bad live switch with:

```bash
sudo nixos-rebuild switch --rollback
```

For an unbootable generation, use the OVH console to choose an older NixOS
boot entry. If SSH and the boot menu are both unavailable, boot OVH rescue,
reconfirm disk identities, mount the installed filesystems for diagnosis, and
restore operator access. Re-running `nixos-anywhere` is destructive and should
be the last recovery option.

Rotate a runtime secret by atomically installing its replacement at the same
path and restarting the consumer. Restart `opencode` after replacing its
password or GitHub token, and the cloudflared tunnel unit after replacing
tunnel credentials. Revoke the old credential at its provider after
validation.

## Adding Development Tooling

Add stable command-line tools and language servers to
`hosts/opencode/development-home.nix`, which is shared by the administrator and
bot. Add a fast-moving tool to the unstable package set only when there is a
concrete need; keep the stable base and no-unfree policy.
OpenCode's automatic LSP downloads are disabled, so every required server must
be present in the Nix profile and any custom extension mapping belongs in
`modules/opencode.nix`.

Update `inputs.helix-config` when editor configuration changes. The JetLS and
JuliaFormatter launchers are pinned in `pkgs/default.nix`; on first use they
install the pinned Julia app into the current user's Julia depot using free
nixpkgs Julia. Change those pins explicitly and test startup when upgrading.

Agent shells have general outbound access for source and package retrieval.
Keep credentials destination-scoped in Sandbox Runtime rather than passing
their real values into the sandbox. Avoid broadening filesystem access or
enabling local binding, SSH, or Unix sockets.

Validate every tooling change:

```bash
nix fmt
nix flake check
nix build .#agent-ci .#github-bridge .#opencode-git \
  .#opencode-workspace .#sandbox-exec .#opencode-server
```

Do not install persistent system tools with `curl | sh`, `npm -g`, rootful
Docker, or manual files under `/etc`. Put reproducible host changes in this
flake and deploy them through `nixos-rebuild`.

## License

This project is available under the [MIT License](LICENSE).
