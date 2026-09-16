{
  lib,
  localPackages,
  settings,
  ...
}:
{
  virtualisation.docker = {
    enable = false;
    rootless = {
      enable = true;
      setSocketVariable = false;
    };
  };

  systemd.user.services.docker.unitConfig.ConditionUser = lib.mkForce "ci-runner";

  security.sudo.extraRules = [
    {
      users = [ settings.accounts.bot.name ];
      runAs = "ci-runner";
      commands = [
        {
          command = "${localPackages.agent-ci}/bin/agent-ci";
          options = [ "NOPASSWD" ];
        }
      ];
    }
  ];

  environment.systemPackages = [ localPackages.agent-ci ];
}
