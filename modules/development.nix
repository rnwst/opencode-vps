{ pkgs, ... }:
{
  environment.systemPackages = with pkgs; [
    acl
    bash
    bubblewrap
    docker-client
    git
    git-lfs
    kitty.terminfo
    socat
  ];

  programs.nix-ld.enable = true;
}
