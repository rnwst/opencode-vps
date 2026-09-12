{ lib, settings }:
{
  enable = true;
  # Keep deployment domains in the local settings override, not this template.
  domain = lib.concatStringsSep "." (
    lib.tail (lib.splitString "." (settings.publicHostName or "opencode.example.com"))
  );
  gatewayPort = 4080;
  runtimeRoot = "/run/opencode-previews";
  maxRuntimes = 4;
  maxPorts = 128;
  maxConnections = 128;
  memoryMax = 2147483648;
  tasksMax = 512;
  cpuQuota = 200;
  maxLifetimeSeconds = 86400;
  idleTimeoutSeconds = 300;
}
// (settings.previews or { })
