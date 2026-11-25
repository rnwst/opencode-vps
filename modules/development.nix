{
  inputs,
  localPackages,
  pkgs,
  ...
}:
{
  environment.systemPackages = with pkgs; [
    acl
    alejandra
    bash
    bash-language-server
    bat
    black
    bubblewrap
    bun
    cargo
    clang-tools
    cmake
    deadnix
    deno
    docker-client
    fd
    ffmpeg
    fish-lsp
    gcc
    gdb
    gh
    git
    git-lfs
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
    kitty.terminfo
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
    openbox
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
    socat
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
    xdotool
    xvfb
    yaml-language-server
    yazi
    zip
    localPackages.jetls
    localPackages.jlfmt
    localPackages.searchfox-cli
  ];

  home-manager.sharedModules = [
    {
      home.sessionVariables = {
        EDITOR = "hx";
        VISUAL = "hx";
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
  ];

  fonts = {
    fontconfig.enable = true;
    packages = [ pkgs.dejavu_fonts ];
  };

  programs.nix-ld = {
    enable = true;
    libraries = with pkgs; [
      # ELF loader and C/C++ runtime.
      glibc
      stdenv.cc.cc.lib

      # GTK and accessibility (ATK and at-spi2-atk are part of at-spi2-core).
      gtk3
      glib
      pango
      cairo
      gdk-pixbuf
      at-spi2-core

      # Fonts and IPC.
      fontconfig
      freetype
      dbus

      # X11.
      libX11
      libXcomposite
      libXcursor
      libXdamage
      libXext
      libXfixes
      libXi
      libXrandr
      libXrender
      libXt
      libxcb

      # Keyboard, Wayland, graphics, and audio.
      libxkbcommon
      wayland
      libdrm
      mesa
      libglvnd
      alsa-lib
    ];
  };
}
