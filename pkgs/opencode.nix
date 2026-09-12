{ pkgsUnstable }:
pkgsUnstable.opencode.overrideAttrs (
  finalAttrs: oldAttrs: {
    version = "1.18.30";
    src = pkgsUnstable.fetchFromGitHub {
      owner = "anomalyco";
      repo = "opencode";
      tag = "v${finalAttrs.version}";
      hash = "sha256-G4qRDwJ6i5SpsiHoej31HRPLOHpkLc66B+UmdzOY1+o=";
    };
    # Keep the pinned recipe, dependencies and stable database channel. Updating
    # nixpkgs wholesale would also change the sandbox and unrelated host packages.
    passthru = oldAttrs.passthru // {
      node_modules = oldAttrs.passthru.node_modules.overrideAttrs {
        inherit (finalAttrs) version src;
        outputHash = "sha256-38HGR+n9I7QrE4i+CmRViX4/3TEQjLgU81LbEDBzj7Y=";
      };
    };
  }
)
