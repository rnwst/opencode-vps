{ lib, settings }:
let
  publicDomain = lib.concatStringsSep "." (
    lib.tail (lib.splitString "." (settings.publicHostName or "opencode.example.com"))
  );
in
assert lib.assertMsg (
  !((settings.previews or { }) ? cpuQuota)
) "previews.cpuQuota is no longer supported; runtimes use weighted CPU sharing.";
{
  enable = true;
  # Keep deployment domains in the local settings override, not this template.
  domain = publicDomain;
  directoryHost = "previews.${publicDomain}";
  gatewayPort = 4080;
  runtimeRoot = "/run/opencode-previews";
  maxRuntimes = 4;
  maxExecs = 4;
  maxPorts = 128;
  maxConnections = 128;
  memoryMax = 9663676416;
  tasksMax = 1024;
  maxLifetimeSeconds = 86400;
  idleTimeoutSeconds = 300;
}
// (settings.previews or { })
