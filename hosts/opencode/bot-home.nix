{ pkgs, settings, ... }:
{
  home = {
    username = settings.accounts.bot.name;
    homeDirectory = "/home/${settings.accounts.bot.name}";
    stateVersion = "26.05";
  };

  programs.git = {
    settings = {
      user = settings.accounts.bot.git;
      committer = settings.accounts.bot.git;
      credential."https://github.com".helper = "!${pkgs.gh}/bin/gh auth git-credential";
      url."https://github.com/" = {
        insteadOf = [
          "git@github.com:"
          "ssh://git@github.com/"
        ];
      };
    };
    # The more specific task override must follow the workspace default.
    # Home Manager writes these includes into the sandbox-readable Nix store.
    includes = [
      {
        condition = "gitdir:${settings.workspacesRoot}/";
        contents.author = settings.accounts.admin.git;
      }
      {
        condition = "gitdir:${settings.workspacesRoot}/.tasks/";
        contents.author = settings.accounts.bot.git;
      }
    ];
  };
}
