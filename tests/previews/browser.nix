{ pkgs, previewPackage }:
pkgs.runCommand "opencode-preview-browser-tests"
  {
    nativeBuildInputs = [
      pkgs.openssl
      (pkgs.python3.withPackages (p: [
        p.aiohttp
        p.playwright
      ]))
    ];
    OPENCODE_PREVIEW_SOURCE = previewPackage.source;
    OPENCODE_PREVIEW_CHROMIUM = "${pkgs.chromium}/bin/chromium";
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1";
    FONTCONFIG_FILE = pkgs.makeFontsConf { fontDirectories = [ pkgs.dejavu_fonts ]; };
    # The Chromium wrapper otherwise attempts unshare in the Nix sandbox.
    NIXOS_CHROMIUM_NO_SANDBOX = "1";
  }
  ''
    export HOME="$TMPDIR/home"
    export XDG_CONFIG_HOME="$HOME/.config"
    export XDG_CACHE_HOME="$HOME/.cache"
    export PYTHONDONTWRITEBYTECODE=1
    mkdir -p "$HOME"
    python3 ${./browser.py}
    touch "$out"
  ''
