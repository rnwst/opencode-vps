{
  pkgs,
  plugin,
  settings,
}:

pkgs.runCommand "opencode-plugin-tests"
  {
    nativeBuildInputs = [ pkgs.nodejs ];
    OPENCODE_PLUGIN = plugin;
    WORKSPACES_ROOT = settings.workspacesRoot;
    WORKSPACES_TMP_ROOT = settings.workspacesTmpRoot;
  }
  ''
    node --experimental-vm-modules --test ${./plugin.test.mjs}
    touch "$out"
  ''
