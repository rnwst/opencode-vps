{ pkgs, ... }:
{
  imports = [ ./development-home.nix ];

  home = {
    username = "rnwst-bot";
    homeDirectory = "/home/rnwst-bot";
    stateVersion = "26.05";
  };

  programs.git.settings = {
    user = {
      name = "rnwst-bot";
      email = "rnwst-bot@users.noreply.github.com";
    };
    credential."https://github.com".helper = "!${pkgs.gh}/bin/gh auth git-credential";
    url."https://github.com/" = {
      insteadOf = [
        "git@github.com:"
        "ssh://git@github.com/"
      ];
    };
  };
}
