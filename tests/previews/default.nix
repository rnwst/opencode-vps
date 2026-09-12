{ pkgs }:
pkgs.runCommand "opencode-preview-tests"
  {
    nativeBuildInputs = [ (pkgs.python3.withPackages (p: [ p.aiohttp ])) ];
  }
  ''
    export PYTHONDONTWRITEBYTECODE=1
    mkdir -p tests/previews pkgs/opencode-preview
    cp ${../../pkgs/opencode-preview/gateway.py} pkgs/opencode-preview/gateway.py
    cp ${../../pkgs/opencode-preview/manager.py} pkgs/opencode-preview/manager.py
    cp ${../../pkgs/opencode-preview/client.py} pkgs/opencode-preview/client.py
    cp ${../../pkgs/opencode-preview/launch.py} pkgs/opencode-preview/launch.py
    cp ${../../pkgs/opencode-preview/supervisor.py} pkgs/opencode-preview/supervisor.py
    cp ${./test_supervisor.py} tests/previews/test_supervisor.py
    cp ${./test_manager.py} tests/previews/test_manager.py
    cp ${./test_gateway.py} tests/previews/test_gateway.py
    python3 -m unittest discover -s tests/previews -v
    touch "$out"
  ''
