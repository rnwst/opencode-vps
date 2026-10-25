{
  pkgs,
  sandboxRuntime,
  sandboxIsolate,
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
      seccomp.applyPath = "${sandboxIsolate}/bin/opencode-sandbox-isolate";
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
    OPENCODE_SANDBOX_WORKSPACE = "/tmp/mcp-workspace";
  }
  ''
    export PYTHONDONTWRITEBYTECODE=1
    mkdir -p /tmp/mcp-workspace/.git /tmp/claude
    printf 'protected fixture\n' > /tmp/mcp-workspace/.git/config
    cd /tmp/mcp-workspace
    # Exercise the production local-IPC filesystem view and native isolation.
    ${sandboxRuntime}/bin/srt --settings ${settings} -- python3 ${./mcp-browser.py}
    touch "$out"
  ''
