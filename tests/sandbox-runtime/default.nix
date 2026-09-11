{ pkgs, sandboxRuntime }:
pkgs.runCommand "sandbox-runtime-security-tests"
  {
    nativeBuildInputs = [ pkgs.nodejs ];
    SRT_PACKAGE = "${sandboxRuntime}/lib/node_modules/@anthropic-ai/sandbox-runtime";
  }
  ''
    node --test ${./network.test.mjs} ${./hardening.test.mjs}
    touch "$out"
  ''
