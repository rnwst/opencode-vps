{
  inputs,
  lib,
  localPackages,
  modulesPath,
  pkgs,
  settings,
  ...
}:
{
  imports = [
    (modulesPath + "/profiles/qemu-guest.nix")
    ./disko.nix
    ./hardware-configuration.nix
    ../../modules/development.nix
    ../../modules/github-bridge.nix
    ../../modules/opencode.nix
    ../../modules/opencode-previews.nix
    ../../modules/opencode-workspaces.nix
    ../../modules/ci-runner.nix
  ];

  assertions = [
    {
      assertion = settings.accounts.admin.name != settings.accounts.bot.name;
      message = "The administrator and bot must use distinct account names.";
    }
    {
      assertion = settings.operatorKeys != [ ];
      message = "Configure settings.operatorKeys with at least one SSH public key before deployment.";
    }
    {
      assertion = settings.githubBridge.maxConcurrentTasks > 0;
      message = "githubBridge.maxConcurrentTasks must be positive.";
    }
    {
      assertion =
        settings.githubBridge.minimumFreePercent > 0 && settings.githubBridge.minimumFreePercent < 100;
      message = "githubBridge.minimumFreePercent must be between 1 and 99.";
    }
    {
      assertion = settings.githubBridge.retentionDays > 0;
      message = "githubBridge.retentionDays must be positive.";
    }
  ];

  networking = {
    inherit (settings) hostName;
    useDHCP = lib.mkDefault true;
    firewall = {
      enable = true;
      allowedTCPPorts = [ 22 ];
    };
  };

  time.timeZone = "UTC";
  console.keyMap = "us";
  i18n.defaultLocale = "en_US.UTF-8";

  boot = {
    loader = {
      grub = {
        enable = true;
        efiSupport = true;
        efiInstallAsRemovable = true;
      };
      efi.canTouchEfiVariables = false;
    };
    kernel.sysctl."kernel.unprivileged_userns_clone" = 1;
  };

  zramSwap = {
    enable = true;
    memoryPercent = 25;
    algorithm = "zstd";
    priority = 100;
  };

  nix = {
    settings = {
      experimental-features = [
        "nix-command"
        "flakes"
      ];
      auto-optimise-store = true;
      trusted-users = [ "root" ];
    };
    gc = {
      automatic = true;
      dates = "weekly";
      options = "--delete-older-than 14d";
    };
  };

  nixpkgs.config.allowUnfree = false;

  programs.fish = {
    enable = true;
    package = localPackages.fish;
  };

  users = {
    mutableUsers = false;
    groups.agent-workspaces = { };

    users = {
      root.hashedPassword = "!";

      ${settings.accounts.admin.name} = {
        isNormalUser = true;
        description = "OpenCode host administrator";
        shell = localPackages.fish;
        extraGroups = [
          "agent-workspaces"
          "wheel"
        ];
        openssh.authorizedKeys.keys = settings.operatorKeys;
      };

      ${settings.accounts.bot.name} = {
        isNormalUser = true;
        description = "OpenCode agent account";
        shell = localPackages.fish;
        extraGroups = [ "agent-workspaces" ];
        openssh.authorizedKeys.keys = settings.operatorKeys;
      };

      ci-runner = {
        isNormalUser = true;
        description = "Credential-free rootless CI runner";
        home = "/var/lib/ci-runner";
        createHome = true;
        shell = "${pkgs.shadow}/bin/nologin";
        linger = true;
        autoSubUidGidRange = true;
        extraGroups = [ "agent-workspaces" ];
      };
    };
  };

  services.openssh = {
    enable = true;
    openFirewall = false;
    settings = {
      PermitRootLogin = "no";
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      AllowUsers = [
        settings.accounts.admin.name
        settings.accounts.bot.name
      ];
    };
  };

  security.sudo = {
    enable = true;
    wheelNeedsPassword = false;
  };

  systemd.tmpfiles.rules = [
    "d ${settings.workspacesRoot} 2750 ${settings.accounts.bot.name} agent-workspaces -"
    "a+ ${settings.workspacesRoot} - - - - u:${settings.accounts.admin.name}:rwx,u:${settings.accounts.bot.name}:rwx,g::r-x,m::rwx,o::---,d:u::rwx,d:u:${settings.accounts.admin.name}:rwx,d:u:${settings.accounts.bot.name}:rwx,d:g::r-x,d:m::rwx,d:o::---"
    "a+ /home/${settings.accounts.bot.name} - - - - u:${settings.accounts.admin.name}:rwx,d:u::rwx,d:u:${settings.accounts.admin.name}:rwx,d:u:${settings.accounts.bot.name}:rwx,d:g::---,d:m::rwx,d:o::---"
    "d /var/lib/ci-runner/jobs 0700 ci-runner users 1d"
    "d ${settings.secrets.directory} 0700 root root -"
  ];

  environment.systemPackages = with pkgs; [
    curl
    git
    vim
  ];

  home-manager = {
    useGlobalPkgs = true;
    useUserPackages = true;
    backupFileExtension = "hm-backup";
    extraSpecialArgs = {
      inherit inputs localPackages settings;
    };
    users.${settings.accounts.bot.name} = import ./home.nix;
    users.${settings.accounts.admin.name} = import ./admin-home.nix;
  };

  system.stateVersion = "26.05";
}
