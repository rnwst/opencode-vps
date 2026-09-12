{
  description = "NixOS OpenCode agent host for an OVHcloud VPS";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    nixpkgs-unstable.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

    disko = {
      url = "github:nix-community/disko";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    home-manager = {
      url = "github:nix-community/home-manager/release-26.05";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    fish-helix = {
      url = "github:rnwst/fish-helix";
      flake = false;
    };

    helix-config = {
      url = "github:rnwst/helix-config/master";
      flake = false;
    };

  };

  outputs =
    inputs@{
      self,
      nixpkgs,
      nixpkgs-unstable,
      disko,
      home-manager,
      ...
    }:
    let
      system = "x86_64-linux";
      settings = import ./hosts/opencode/settings.nix;
      pkgs = import nixpkgs {
        inherit system;
        config.allowUnfree = false;
      };
      pkgsUnstable = import nixpkgs-unstable {
        inherit system;
        config.allowUnfree = false;
      };
      localPackages = import ./pkgs {
        inherit
          pkgs
          pkgsUnstable
          settings
          ;
      };
    in
    {
      nixosConfigurations.opencode = nixpkgs.lib.nixosSystem {
        inherit system;
        specialArgs = {
          inherit inputs localPackages settings;
        };
        modules = [
          disko.nixosModules.disko
          home-manager.nixosModules.home-manager
          ./hosts/opencode
        ];
      };

      packages.${system} = localPackages // {
        default = localPackages.agent-ci;
      };

      checks.${system} = {
        github-bridge = import ./tests/github-bridge { inherit pkgs; };
        github-bridge-vm = import ./tests/nixos/github-bridge.nix {
          inherit localPackages pkgs;
        };
        sandbox-runtime = import ./tests/sandbox-runtime {
          inherit pkgs;
          sandboxRuntime = localPackages.sandbox-runtime;
        };
        sandbox-vm = import ./tests/nixos/sandbox.nix {
          inherit localPackages pkgs;
        };
        previews = import ./tests/previews { inherit pkgs; };
        previews-vm = import ./tests/nixos/previews.nix { inherit pkgs pkgsUnstable; };
        previews-browser = import ./tests/previews/browser.nix {
          inherit pkgs;
          previewPackage = localPackages.opencode-preview;
        };
        nixos = self.nixosConfigurations.opencode.config.system.build.toplevel;
      };

      formatter.${system} = pkgs.nixfmt-tree;

      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          deadnix
          nixfmt
          ruff
          statix
        ];
      };
    };
}
