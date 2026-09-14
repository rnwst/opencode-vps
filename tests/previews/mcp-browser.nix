{
  pkgs,
  sandboxRuntime,
  playwrightMcp ? import ../../pkgs/playwright-mcp.nix { inherit pkgs; },
}:
let
  settings = pkgs.writeText "mcp-browser-srt.json" (
    builtins.toJSON {
      filesystem = {
        denyRead = [ "/build" ];
        allowRead = [ ];
        allowWrite = [ "/tmp" ];
        denyWrite = [ ];
      };
      network = {
        allowedDomains = [ ];
        deniedDomains = [ "*:22" ];
        strictAllowlist = false;
        allowLocalBinding = false;
        tlsTerminate = { };
      };
      bwrapPath = "${pkgs.bubblewrap}/bin/bwrap";
      socatPath = "${pkgs.socat}/bin/socat";
      seccomp.applyPath = "${sandboxRuntime}/lib/node_modules/@anthropic-ai/sandbox-runtime/vendor/seccomp/x64/apply-seccomp";
      ripgrep.command = "${pkgs.ripgrep}/bin/rg";
    }
  );
in
pkgs.runCommand "opencode-playwright-mcp-browser-tests"
  {
    nativeBuildInputs = [
      pkgs.bash
      (pkgs.python3.withPackages (p: [ p.cryptography ]))
    ];
    OPENCODE_PLAYWRIGHT_MCP = "${playwrightMcp}/bin/opencode-playwright-mcp";
    OPENCODE_UNSHARE = "${pkgs.util-linux}/bin/unshare";
  }
  ''
    export PYTHONDONTWRITEBYTECODE=1
    mkdir -p /tmp/mcp-workspace/.git /tmp/claude
    printf 'protected fixture\n' > /tmp/mcp-workspace/.git/config
    cd /tmp/mcp-workspace
    # Exercise the real SRT bwrap filesystem/network policy and AF_UNIX filter.
    ${sandboxRuntime}/bin/srt --settings ${settings} -- python3 ${./mcp-browser.py}
    touch "$out"
  ''
