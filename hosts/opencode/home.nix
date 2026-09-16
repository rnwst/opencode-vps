{ pkgs, settings, ... }:
{
  imports = [ ./development-home.nix ];

  home = {
    username = settings.accounts.bot.name;
    homeDirectory = "/home/${settings.accounts.bot.name}";
    stateVersion = "26.05";
  };

  programs.git.settings = {
    user = settings.accounts.bot.git;
    credential."https://github.com".helper = "!${pkgs.gh}/bin/gh auth git-credential";
    url."https://github.com/" = {
      insteadOf = [
        "git@github.com:"
        "ssh://git@github.com/"
      ];
    };
  };
}
