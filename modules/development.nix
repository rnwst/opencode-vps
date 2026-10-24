{ localPackages, pkgs, ... }:
{
  environment.systemPackages = with pkgs; [
    acl
    bash
    bubblewrap
    docker-client
    git
    git-lfs
    kitty.terminfo
    localPackages.searchfox-cli
    socat
    xdotool
    xvfb
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
