{
  inputs,
  localPackages,
  pkgs,
  ...
}:
{
  home = {
    sessionVariables = {
      EDITOR = "hx";
      VISUAL = "hx";
    };
    packages = with pkgs; [
      alejandra
      bash-language-server
      bat
      black
      bun
      cargo
      clang-tools
      cmake
      deadnix
      deno
      fd
      ffmpeg
      fish-lsp
      gcc
      gdb
      gh
      gnumake
      go
      gopls
      gotools
      gradle
      haskell-language-server
      helix
      htop
      jdk
      jq
      just
      julia
      lazygit
      lemminx
      lua
      lua-language-server
      marksman
      maven
      nil
      nixd
      nixfmt
      nodejs
      perl
      pkg-config
      prettier
      python3
      pyright
      ripgrep
      ruff
      rust-analyzer
      rustc
      shellcheck
      shfmt
      statix
      stylua
      taplo
      tmux
      tree
      typescript
      typescript-language-server
      unzip
      uv
      vscode-langservers-extracted
      wget
      yaml-language-server
      yazi
      zip
      localPackages.jetls
      localPackages.jlfmt
    ];
  };

  xdg.configFile = {
    "fish/functions/fish_bind_count.fish".source =
      inputs.fish-helix + "/functions/fish_bind_count.fish";
    "fish/functions/fish_default_mode_prompt.fish".source =
      inputs.fish-helix + "/functions/fish_default_mode_prompt.fish";
    "fish/functions/fish_helix_command.fish".source =
      inputs.fish-helix + "/functions/fish_helix_command.fish";
    "fish/functions/fish_helix_key_bindings.fish".source =
      inputs.fish-helix + "/functions/fish_helix_key_bindings.fish";
    "helix/config.toml".source = inputs.helix-config + "/config.toml";
    "helix/languages.toml".source = inputs.helix-config + "/languages.toml";
  };

  programs = {
    direnv = {
      enable = true;
      nix-direnv.enable = true;
    };

    fish = {
      enable = true;
      package = localPackages.fish;
      interactiveShellInit = ''
        fish_helix_key_bindings
      '';
      shellAbbrs = {
        g = "git";
        ga = "git add";
        gc = "git commit";
        gd = "git diff";
        gl = "git log --oneline --decorate -20";
        gs = "git status";
        h = "hx";
        lg = "lazygit";
        oc = "opencode";
        s = "sudo";
        y = "yazi";
      };
    };

    git = {
      enable = true;
      lfs.enable = true;
      settings.init.defaultBranch = "main";
    };

    home-manager.enable = true;

    starship = {
      enable = true;
      enableFishIntegration = true;
    };
  };
}
