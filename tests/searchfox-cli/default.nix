{ pkgs, searchfoxCli }:
pkgs.runCommand "searchfox-cli-tls-tests"
  {
    nativeBuildInputs = [
      pkgs.openssl
      pkgs.python3
      searchfoxCli
    ];
  }
  ''
    export HOME="$TMPDIR/home"
    mkdir -p "$HOME"
    python ${./tls.py}
    touch "$out"
  ''
