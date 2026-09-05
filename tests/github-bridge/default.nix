{ pkgs }:

pkgs.runCommand "github-bridge-tests"
  {
    nativeBuildInputs = [ pkgs.python3 ];
  }
  ''
    export PYTHONDONTWRITEBYTECODE=1
    export GITHUB_BRIDGE_SOURCE=${../../pkgs/github-bridge/bridge.py}
    python3 ${./test_bridge.py}
    touch $out
  ''
