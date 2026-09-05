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
    "a+ ${settings.githubBridge.canonicalRoot} - - - - u:rnwst-bot:--x,u:rnwst-admin:rwx,m::rwx"
    "d ${settings.workspacesRoot}/.tasks 2750 rnwst-bot agent-workspaces -"
    "a+ ${settings.workspacesRoot}/.tasks - - - - u:rnwst-admin:rwx,u:rnwst-bot:rwx,m::rwx,d:u::rwx,d:u:rnwst-admin:rwx,d:u:rnwst-bot:rwx,d:g::r-x,d:m::rwx,d:o::---"
    "d /run/opencode-workspace/locks 0750 root root -"
  ];
}
