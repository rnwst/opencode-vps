#!/usr/bin/env bash

set -euo pipefail

bot_user=rnwst-bot
bot_group=agent-workspaces
admin_user=rnwst-admin
workspaces_root=${OPENCODE_WORKSPACES_ROOT:?}
tasks_root="$workspaces_root/.tasks"
canonical_root=${OPENCODE_CANONICAL_ROOT:?}
token_file=${OPENCODE_GITHUB_TOKEN_FILE:?}
minimum_free_percent=${OPENCODE_MINIMUM_FREE_PERCENT:-15}
github_api_url=${OPENCODE_GITHUB_API_URL:-https://api.github.com}
workspace_test_mode=${OPENCODE_WORKSPACE_TEST_MODE:-0}
sandbox_exec=${OPENCODE_SANDBOX_EXEC:?}
lock_root=/run/opencode-workspace/locks

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

usage() {
  cat >&2 <<'EOF'
usage:
  opencode-workspace create OWNER/REPOSITORY [WORKSPACE_NAME]
  opencode-workspace init WORKSPACE_NAME
  opencode-workspace set-remote WORKSPACE_NAME OWNER/REPOSITORY
  opencode-workspace publish WORKSPACE_NAME OWNER/REPOSITORY [--visibility public|private]
  opencode-workspace list
  opencode-workspace refresh OWNER/REPOSITORY
  opencode-workspace remove WORKSPACE_NAME [--force]
  opencode-workspace prepare-task TASK_ID OWNER/REPOSITORY ACTION SUBJECT_NUMBER REF
  opencode-workspace restore-task TASK_ID OWNER/REPOSITORY ACTION SUBJECT_NUMBER REF
  opencode-workspace head-task TASK_ID
  opencode-workspace inspect-task TASK_ID
  opencode-workspace remove-task TASK_ID
EOF
  exit 64
}

(( EUID == 0 )) || die "opencode-workspace must be run with sudo"

valid_workspace_name() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] && [[ "$1" != ".tasks" ]]
}

valid_task_id() {
  [[ "$1" =~ ^task-[a-z0-9][a-z0-9-]{7,95}$ ]]
}

valid_repo() {
  [[ "$1" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]
}

valid_action() {
  [[ "$1" =~ ^(answer|implement|review)$ ]]
}

valid_subject_number() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

assert_btrfs() {
  [[ "$(stat -f -c %T "$workspaces_root")" == "btrfs" ]] ||
    die "$workspaces_root must be on Btrfs"
}

assert_free_space() {
  local available total free_percent
  read -r available total < <(df --output=avail,size --block-size=1 "$workspaces_root" | tail -n 1)
  (( total > 0 )) || die "could not determine workspace filesystem size"
  free_percent=$((available * 100 / total))
  (( free_percent >= minimum_free_percent )) ||
    die "workspace filesystem has ${free_percent}% free; ${minimum_free_percent}% is required"
}

read_token() {
  [[ -r "$token_file" ]] || die "GitHub token is unavailable"
  github_token="$(<"$token_file")"
  [[ -n "$github_token" ]] || die "GitHub token is empty"
  export github_token
}

github_api() {
  local method=$1 path=$2 body=${3-}
  local args=(
    --fail-with-body --silent --show-error
    --request "$method"
    --header "Accept: application/vnd.github+json"
    --header "Authorization: Bearer $github_token"
    --header "X-GitHub-Api-Version: 2022-11-28"
    --header "User-Agent: opencode-workspace"
  )
  [[ -z "$body" ]] || args+=(--header "Content-Type: application/json" --data "$body")
  curl "${args[@]}" "$github_api_url$path"
}

validate_clone_url() {
  local url=$1 full_name=$2
  if [[ "$github_api_url" == "https://api.github.com" ]]; then
    [[ "$url" == "https://github.com/$full_name.git" ]]
    return
  fi
  [[ "$workspace_test_mode" == 1 && "$url" == file://* ]]
}

git_as_bot() {
  local token=$github_token
  # shellcheck disable=SC2016
  local helper='!f() { if [ "$1" = get ]; then printf "%s\n" "username=x-access-token" "password=$OPENCODE_GITHUB_TOKEN"; fi; }; f'
  runuser --user "$bot_user" --group "$bot_group" -- env \
    GIT_CONFIG_GLOBAL=/dev/null \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_TERMINAL_PROMPT=0 \
    HOME="/home/$bot_user" \
    OPENCODE_GITHUB_TOKEN="$token" \
    XDG_CONFIG_HOME="/home/$bot_user/.config" \
    git -c credential.helper= \
    -c credential.https://github.com.helper="$helper" \
    "$@"
}

git_as_bot_no_auth() {
  runuser --user "$bot_user" --group "$bot_group" -- env \
    GIT_CONFIG_GLOBAL=/dev/null \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_TERMINAL_PROMPT=0 \
    HOME="/home/$bot_user" \
    XDG_CONFIG_HOME="/home/$bot_user/.config" \
    git "$@"
}

sandbox_git() {
  local directory=$1 command=$2
  # shellcheck disable=SC2016
  runuser --user "$bot_user" --group "$bot_group" -- \
    env -u CREDENTIALS_DIRECTORY HOME="/home/$bot_user" \
    bash -c 'cd -- "$1"; exec "$2" -c "$3"' bash \
    "$directory" "$sandbox_exec" "$command"
}

sandbox_git_status() {
  # shellcheck disable=SC2016
  sandbox_git "$1" 'paths=(.); for path in .[^.]*; do [[ -c "$path" ]] && paths+=(":(exclude)$path"); done; git status --porcelain=v1 --untracked-files=all -- "${paths[@]}"'
}

remote_contains_head() {
  local directory=$1 head url verify
  head="$(sandbox_git "$directory" 'git rev-parse --verify HEAD')"
  while IFS= read -r url; do
    [[ -n "$url" ]] || continue
    if [[ "$workspace_test_mode" != 1 ]]; then
      [[ "$url" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git$ ]] || continue
    fi
    verify="$(mktemp -d)"
    chown "$bot_user:$bot_group" "$verify"
    git_as_bot init --bare --quiet "$verify"
    if git_as_bot -C "$verify" fetch --quiet --no-tags --prune -- "$url" \
      '+refs/heads/*:refs/remotes/verify/*' >/dev/null 2>&1 &&
      git_as_bot -C "$verify" branch -r --contains "$head" 2>/dev/null | grep -q .; then
      rm -rf "$verify"
      return 0
    fi
    rm -rf "$verify"
  done < <(sandbox_git "$directory" 'git remote get-url --all origin; git remote get-url --all upstream 2>/dev/null || true')
  return 1
}

resolve_repo() {
  valid_repo "$1" || die "invalid repository: $1"
  repo_json="$(github_api GET "/repos/$1")" || die "GitHub repository is unavailable: $1"
  repo_id="$(jq -er '.id | tostring' <<<"$repo_json")"
  repo_full_name="$(jq -er '.full_name' <<<"$repo_json")"
  repo_name="$(jq -er '.name' <<<"$repo_json")"
  default_branch="$(jq -er '.default_branch // empty' <<<"$repo_json")"
  clone_url="$(jq -er '.clone_url' <<<"$repo_json")"
  repo_push="$(jq -r '.permissions.push // false' <<<"$repo_json")"
  validate_clone_url "$clone_url" "$repo_full_name" ||
    die "GitHub returned an unexpected clone URL"
}

apply_workspace_acl() {
  local path=$1
  chown "$bot_user:$bot_group" "$path"
  chmod 2750 "$path"
  setfacl -R -m "u:$admin_user:rwX,u:$bot_user:rwX,g::r-X,m::rwx,o::---" "$path"
  find "$path" -type d -exec setfacl -m \
    "d:u::rwx,d:u:$admin_user:rwx,d:u:$bot_user:rwx,d:g::r-x,d:m::rwx,d:o::---" {} +
}

canonical_update() {
  local base="$canonical_root/$repo_id"
  local lock="$lock_root/$repo_id.lock"

  install -d -m 0750 -o root -g "$bot_group" "$canonical_root"
  install -d -m 0750 -o root -g root "$lock_root"
  exec 9>"$lock"
  flock 9

  if [[ ! -e "$base" ]]; then
    btrfs subvolume create "$base" >/dev/null
    chown "$bot_user:$bot_group" "$base"
    chmod 0750 "$base"
    if ! git_as_bot clone --origin origin -- "$clone_url" "$base"; then
      btrfs subvolume delete "$base" >/dev/null || true
      die "failed to clone canonical repository"
    fi
    apply_workspace_acl "$base"
  fi

  btrfs subvolume show "$base" >/dev/null 2>&1 || die "canonical repository is not a Btrfs subvolume"
  git_as_bot -C "$base" remote set-url origin "$clone_url"
  git_as_bot -C "$base" fetch --prune origin
  if [[ -n "$default_branch" ]]; then
    git_as_bot -C "$base" checkout -B "$default_branch" "origin/$default_branch" >/dev/null
    git_as_bot -C "$base" reset --hard "origin/$default_branch" >/dev/null
    git_as_bot -C "$base" clean -ffdx >/dev/null
  fi
  [[ -z "$(git_as_bot -C "$base" status --porcelain)" ]] || die "canonical repository is not clean"
  canonical_path=$base
}

snapshot_workspace() {
  local destination=$1
  [[ ! -e "$destination" ]] || die "destination already exists: $destination"
  install -d -m 2750 -o "$bot_user" -g "$bot_group" "$(dirname "$destination")"
  btrfs subvolume snapshot "$canonical_path" "$destination" >/dev/null
  apply_workspace_acl "$destination"
}

checkout_ref() {
  local destination=$1 ref=$2 action=${3-} number=${4-}
  local commit
  case "$ref" in
    default)
      [[ -n "$default_branch" ]] || die "repository has no default branch"
      commit="origin/$default_branch"
      ;;
    pull/*)
      local pr_number=${ref#pull/}
      valid_subject_number "$pr_number" || die "invalid pull request ref"
      git_as_bot -C "$destination" fetch origin "pull/$pr_number/head:refs/remotes/origin/pr/$pr_number"
      commit="refs/remotes/origin/pr/$pr_number"
      ;;
    sha/*)
      commit=${ref#sha/}
      [[ "$commit" =~ ^[0-9a-fA-F]{40}$ ]] || die "invalid commit SHA"
      git_as_bot -C "$destination" fetch origin "$commit"
      ;;
    *) die "invalid task ref: $ref" ;;
  esac

  if [[ -n "$action" ]]; then
    local task_suffix=${destination##*/}
    # Informative task directory names can be long; branches need only the
    # deterministic hash suffix to remain unique.
    task_suffix=${task_suffix##*-}
    local branch="${action}/${number}-${task_suffix}"
    git_as_bot -C "$destination" checkout -B "$branch" "$commit" >/dev/null
  else
    git_as_bot -C "$destination" checkout --detach "$commit" >/dev/null
  fi
}

ensure_push_remote() {
  local destination=$1
  [[ "$repo_push" == "true" ]] && return 0

  local bot_login fork_full fork_json
  bot_login="$(github_api GET /user | jq -er .login)"
  fork_full="$bot_login/$repo_name"
  if ! fork_json="$(github_api GET "/repos/$fork_full" 2>/dev/null)"; then
    github_api POST "/repos/$repo_full_name/forks" '{}' >/dev/null
    for _ in $(seq 1 15); do
      sleep 2
      if fork_json="$(github_api GET "/repos/$fork_full" 2>/dev/null)"; then
        break
      fi
    done
  fi
  [[ -n "${fork_json:-}" ]] || die "fork did not become available: $fork_full"
  local fork_parent_id
  fork_parent_id="$(jq -er '.parent.id | tostring' <<<"$fork_json")" ||
    die "$fork_full exists but is not a fork of $repo_full_name"
  [[ "$fork_parent_id" == "$repo_id" ]] ||
    die "$fork_full is not a fork of $repo_full_name"

  git_as_bot -C "$destination" remote rename origin upstream
  git_as_bot -C "$destination" remote add origin "https://github.com/$fork_full.git"
}

manual_path() {
  valid_workspace_name "$1" || die "invalid workspace name: $1"
  workspace_path="$workspaces_root/$1"
}

task_path() {
  valid_task_id "$1" || die "invalid task ID: $1"
  workspace_path="$tasks_root/$1"
}

command_create() {
  (( $# >= 1 && $# <= 2 )) || usage
  read_token
  resolve_repo "$1"
  local name=${2:-$repo_name}
  manual_path "$name"
  assert_btrfs
  assert_free_space
  canonical_update
  snapshot_workspace "$workspace_path"
  if ! ensure_push_remote "$workspace_path"; then
    btrfs subvolume delete "$workspace_path" >/dev/null || true
    exit 1
  fi
  printf '%s\n' "$workspace_path"
}

command_init() {
  (( $# == 1 )) || usage
  manual_path "$1"
  assert_btrfs
  assert_free_space
  [[ ! -e "$workspace_path" ]] || die "workspace already exists: $workspace_path"
  btrfs subvolume create "$workspace_path" >/dev/null
  chown "$bot_user:$bot_group" "$workspace_path"
  chmod 2750 "$workspace_path"
  git_as_bot_no_auth -C "$workspace_path" init --initial-branch=main
  apply_workspace_acl "$workspace_path"
  printf '%s\n' "$workspace_path"
}

command_set_remote() {
  (( $# == 2 )) || usage
  manual_path "$1"
  [[ -d "$workspace_path/.git" ]] || die "manual workspace is not a Git repository"
  [[ ! -e "$workspace_path/.git/gitdir" ]] || die "linked Git worktrees are unsupported"
  read_token
  resolve_repo "$2"
  if git_as_bot -C "$workspace_path" remote get-url origin >/dev/null 2>&1; then
    die "origin already exists"
  fi
  git_as_bot -C "$workspace_path" remote add origin "$clone_url"
}

command_publish() {
  (( $# == 2 || $# == 4 )) || usage
  manual_path "$1"
  valid_repo "$2" || die "invalid repository: $2"
  local visibility=public
  if (( $# == 4 )); then
    [[ "$3" == "--visibility" ]] || usage
    visibility=$4
  fi
  [[ "$visibility" == "public" || "$visibility" == "private" ]] || die "visibility must be public or private"
  [[ -d "$workspace_path/.git" ]] || die "manual workspace is not a Git repository"
  read_token
  if git_as_bot -C "$workspace_path" remote get-url origin >/dev/null 2>&1; then
    die "origin already exists; use set-remote for an existing repository"
  fi

  local owner=${2%%/*} name=${2#*/} bot_login endpoint body response
  bot_login="$(github_api GET /user | jq -er .login)"
  body="$(jq -n --arg name "$name" --argjson private "$([[ "$visibility" == private ]] && printf true || printf false)" \
    '{name: $name, private: $private}')"
  if [[ "${owner,,}" == "${bot_login,,}" ]]; then
    endpoint=/user/repos
  else
    endpoint="/orgs/$owner/repos"
  fi
  response="$(github_api POST "$endpoint" "$body")" || die "failed to create GitHub repository"
  clone_url="$(jq -er .clone_url <<<"$response")"
  validate_clone_url "$clone_url" "$owner/$name" || die "GitHub returned an unexpected clone URL"
  git_as_bot -C "$workspace_path" remote add origin "$clone_url"
  local pushed=false
  if git_as_bot -C "$workspace_path" rev-parse --verify HEAD >/dev/null 2>&1; then
    local branch
    branch="$(git_as_bot -C "$workspace_path" branch --show-current)"
    [[ -n "$branch" ]] || die "cannot publish a detached HEAD"
    git_as_bot -C "$workspace_path" push --set-upstream origin "$branch"
    pushed=true
  else
    printf '%s\n' "repository created; no commits were available to push"
  fi
  if [[ "$pushed" == true ]]; then
    resolve_repo "$owner/$name"
    canonical_update
  fi
}

command_list() {
  printf 'manual workspaces:\n'
  find "$workspaces_root" -mindepth 1 -maxdepth 1 -type d ! -name .tasks -printf '%f\n' | sort
  printf 'automated tasks:\n'
  find "$tasks_root" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort || true
}

command_refresh() {
  (( $# == 1 )) || usage
  read_token
  resolve_repo "$1"
  assert_btrfs
  canonical_update
  printf '%s\n' "$canonical_path"
}

command_remove() {
  (( $# >= 1 && $# <= 2 )) || usage
  local force=false
  if (( $# == 2 )); then
    [[ "$2" == "--force" ]] || usage
    force=true
  fi
  manual_path "$1"
  [[ -d "$workspace_path/.git" ]] || die "manual workspace is not a Git repository"
  if [[ "$force" == true ]]; then
    printf 'Skipping cleanliness and remote commit verification because --force was supplied.\n'
  else
    [[ -z "$(sandbox_git_status "$workspace_path")" ]] ||
      die "workspace has uncommitted changes"
    if sandbox_git "$workspace_path" 'git rev-parse --verify HEAD' >/dev/null 2>&1; then
      printf 'Verifying recoverability before deletion by fetching remote refs into a temporary repository.\n'
      read_token
      remote_contains_head "$workspace_path" ||
        die "workspace has commits not known to a remote; push them or use --force"
    fi
  fi
  btrfs subvolume delete "$workspace_path" >/dev/null
  printf 'Removed workspace: %s\n' "$workspace_path"
}

command_prepare_task() {
  (( $# == 5 )) || usage
  local task_id=$1 repo=$2 action=$3 number=$4 ref=$5
  valid_action "$action" || die "invalid task action"
  valid_subject_number "$number" || die "invalid subject number"
  task_path "$task_id"
  local marker="$workspace_path/.git/opencode-task.json"
  read_token
  resolve_repo "$repo"
  if [[ -e "$workspace_path" ]]; then
    if [[ -f "$marker" ]] &&
      [[ "$(jq -er .task_id "$marker")" == "$task_id" ]] &&
      [[ "$(jq -er .repository_id "$marker")" == "$repo_id" ]] &&
      [[ "$(jq -er .action "$marker")" == "$action" ]]; then
      jq -n --arg path "$workspace_path" --arg repository "$repo_full_name" --arg id "$repo_id" \
        '{path: $path, repository: $repository, repository_id: $id}'
      return
    fi
    btrfs subvolume delete "$workspace_path" >/dev/null ||
      die "incomplete task destination cannot be removed: $workspace_path"
  fi
  assert_btrfs
  assert_free_space
  canonical_update
  snapshot_workspace "$workspace_path"
  if ! checkout_ref "$workspace_path" "$ref" "$action" "$number"; then
    btrfs subvolume delete "$workspace_path" >/dev/null || true
    exit 1
  fi
  if [[ "$action" == implement ]] && ! ensure_push_remote "$workspace_path"; then
    btrfs subvolume delete "$workspace_path" >/dev/null || true
    exit 1
  fi
  jq -n --arg task_id "$task_id" --arg repository_id "$repo_id" --arg action "$action" \
    '{task_id: $task_id, repository_id: $repository_id, action: $action}' \
    > "$marker"
  chown root:root "$marker"
  chmod 0444 "$marker"
  jq -n --arg path "$workspace_path" --arg repository "$repo_full_name" --arg id "$repo_id" \
    '{path: $path, repository: $repository, repository_id: $id}'
}

command_restore_task() {
  (( $# == 5 )) || usage
  local task_id=$1 repo=$2 action=$3 number=$4 ref=$5
  valid_action "$action" || die "invalid task action"
  valid_subject_number "$number" || die "invalid subject number"
  task_path "$task_id"
  read_token
  resolve_repo "$repo"
  local marker="$workspace_path/.git/opencode-task.json"
  if [[ -e "$workspace_path" ]]; then
    btrfs subvolume show "$workspace_path" >/dev/null 2>&1 &&
      [[ -f "$marker" ]] &&
      [[ "$(jq -er .task_id "$marker")" == "$task_id" ]] &&
      [[ "$(jq -er .repository_id "$marker")" == "$repo_id" ]] &&
      [[ "$(jq -er .action "$marker")" == "$action" ]] ||
      die "existing task workspace does not match its task record"
    printf '%s\n' "$workspace_path"
    return
  fi
  assert_btrfs
  assert_free_space
  canonical_update
  snapshot_workspace "$workspace_path"
  if ! checkout_ref "$workspace_path" "$ref" "$action" "$number"; then
    btrfs subvolume delete "$workspace_path" >/dev/null || true
    exit 1
  fi
  if [[ "$action" == implement ]] && ! ensure_push_remote "$workspace_path"; then
    btrfs subvolume delete "$workspace_path" >/dev/null || true
    exit 1
  fi
  jq -n --arg task_id "$task_id" --arg repository_id "$repo_id" --arg action "$action" \
    '{task_id: $task_id, repository_id: $repository_id, action: $action}' \
    > "$marker"
  chown root:root "$marker"
  chmod 0444 "$marker"
  printf '%s\n' "$workspace_path"
}

command_inspect_task() {
  (( $# == 1 )) || usage
  task_path "$1"
  [[ -d "$workspace_path/.git" ]] || die "task workspace does not exist"
  local dirty=false pushed=true state
  [[ -z "$(sandbox_git_status "$workspace_path")" ]] || dirty=true
  if sandbox_git "$workspace_path" 'git rev-parse --verify HEAD' >/dev/null 2>&1; then
    read_token
    remote_contains_head "$workspace_path" || pushed=false
  fi
  state=clean
  [[ "$dirty" == false ]] || state="dirty-worktree"
  [[ "$dirty" == true || "$pushed" == true ]] || state="unpushed-commits"
  jq -n --arg path "$workspace_path" --arg state "$state" --argjson dirty "$dirty" --argjson pushed "$pushed" \
    '{path: $path, state: $state, dirty: $dirty, pushed: $pushed}'
}

command_head_task() {
  (( $# == 1 )) || usage
  task_path "$1"
  [[ -f "$workspace_path/.git/opencode-task.json" ]] || die "task workspace does not exist"
  sandbox_git "$workspace_path" 'git rev-parse --verify HEAD'
}

command_remove_task() {
  (( $# == 1 )) || usage
  task_path "$1"
  [[ -e "$workspace_path" ]] || exit 0
  btrfs subvolume delete "$workspace_path" >/dev/null
}

command=${1-}
[[ -n "$command" ]] || usage
shift
if [[ "${SUDO_USER:-}" == "github-bridge" ]]; then
  case "$command" in
    prepare-task|restore-task|head-task|inspect-task|remove-task) ;;
    *) die "github-bridge may only use internal task commands" ;;
  esac
fi
case "$command" in
  create) command_create "$@" ;;
  init) command_init "$@" ;;
  set-remote) command_set_remote "$@" ;;
  publish) command_publish "$@" ;;
  list) command_list "$@" ;;
  refresh) command_refresh "$@" ;;
  remove) command_remove "$@" ;;
  prepare-task) command_prepare_task "$@" ;;
  restore-task) command_restore_task "$@" ;;
  head-task) command_head_task "$@" ;;
  inspect-task) command_inspect_task "$@" ;;
  remove-task) command_remove_task "$@" ;;
  *) usage ;;
esac
