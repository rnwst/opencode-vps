{ pkgs }:
pkgs.rustPlatform.buildRustPackage {
  pname = "searchfox-cli";
  version = "0.21.0";

  src = pkgs.fetchFromGitHub {
    owner = "padenot";
    repo = "searchfox-cli";
    tag = "v0.21.0";
    hash = "sha256-IWLVZ13RTAaoGiB/dUdtdg/0wSMd8y0NCkg9HxeT/NY=";
  };

  patches = [ ./searchfox-cli-ca-bundle.patch ];

  cargoHash = "sha256-wORNpdp9Mwkmnr3DcvTLCd1sCNVHRqoMrmZvOqCmbkY=";
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
