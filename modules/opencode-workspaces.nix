{
  localPackages,
  pkgs,
  settings,
  ...
}:
{
  environment.systemPackages = [
    localPackages.opencode-workspace
    pkgs.btrfs-progs
  ];

  systemd.tmpfiles.rules = [
    "d ${settings.githubBridge.canonicalRoot} 0750 root agent-workspaces -"
    "a+ ${settings.githubBridge.canonicalRoot} - - - - u:${settings.accounts.bot.name}:--x,u:${settings.accounts.admin.name}:rwx,m::rwx"
    "d ${settings.workspacesRoot}/.tasks 2750 ${settings.accounts.bot.name} agent-workspaces -"
    "a+ ${settings.workspacesRoot}/.tasks - - - - u:${settings.accounts.admin.name}:rwx,u:${settings.accounts.bot.name}:rwx,m::rwx,d:u::rwx,d:u:${settings.accounts.admin.name}:rwx,d:u:${settings.accounts.bot.name}:rwx,d:g::r-x,d:m::rwx,d:o::---"
    "d ${settings.workspacesTmpRoot} 0711 root root -"
    "d ${settings.workspacesTmpRoot}/.tasks 0711 root root -"
    "d /run/opencode-workspace/locks 0750 root root -"
  ];

  systemd.services.opencode-workspace-temp-init = {
    description = "Create scoped temporary roots for existing OpenCode workspaces";
    wantedBy = [ "multi-user.target" ];
    before = [ "opencode.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      for workspace in ${settings.workspacesRoot}/*; do
        [ -d "$workspace/.git" ] || continue
        ${localPackages.opencode-workspace}/bin/opencode-workspace ensure-temp manual "''${workspace##*/}"
      done
      for workspace in ${settings.workspacesRoot}/.tasks/task-*; do
        [ -d "$workspace/.git" ] || continue
        ${localPackages.opencode-workspace}/bin/opencode-workspace ensure-temp task "''${workspace##*/}"
      done
    '';
  };
}
