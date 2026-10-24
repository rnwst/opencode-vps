{ pkgs }:
pkgs.rustPlatform.buildRustPackage {
  pname = "searchfox-cli";
  version = "0.20.5";

  src = pkgs.fetchFromGitHub {
    owner = "padenot";
    repo = "searchfox-cli";
    rev = "d35ceeabf2258187af672a01072bddca2fffbbef";
    hash = "sha256-dwCzEmvva/Z2WBtHqGkHApsWS12eWMhLf6nRmZ+WLeQ=";
  };

  patches = [ ./searchfox-cli-ca-bundle.patch ];

  cargoHash = "sha256-biYl+hg4o2AUr4cCh1+0UzTnGIUDWYtwDLWXFxwX8gA=";
  cargoBuildFlags = [ "--package=searchfox-cli" ];
  # Integration tests contact searchfox.org; run the offline unit tests instead.
  cargoTestFlags = [
    "--package=searchfox-cli"
    "--package=searchfox-lib"
    "--lib"
    "--bins"
  ];
  # Nix sets SSL_CERT_FILE to a nonexistent file in network-free builds.
  # Runtime CA handling is exercised separately by the TLS fixture check.
  preCheck = ''
    unset SSL_CERT_FILE
  '';

  meta = {
    description = "Command-line interface for searching Mozilla codebases using Searchfox";
    homepage = "https://github.com/padenot/searchfox-cli";
    license = with pkgs.lib.licenses; [
      mit
      asl20
    ];
    mainProgram = "searchfox-cli";
  };
}
