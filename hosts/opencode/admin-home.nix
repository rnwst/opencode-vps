{ settings, ... }:
{
  imports = [ ./development-home.nix ];

  home = {
    username = "rnwst-admin";
    homeDirectory = "/home/rnwst-admin";
    stateVersion = "26.05";
  };

  programs = {
    fish = {
      functions.oca = ''
        if test (count $argv) -lt 1
            echo "usage: oca REPOSITORY [OPENCODE_ATTACH_OPTION...]" >&2
            return 64
        end

        set -l project $argv[1]
        set -e argv[1]
        set -l directory
        if string match -q '/*' -- "$project"
            set directory "$project"
        else
            set directory "${settings.workspacesRoot}/$project"
        end

        if not test -d "$directory"
            echo "workspace does not exist: $directory" >&2
            return 66
        end
        set directory (path resolve "$directory")
        if not string match -q '${settings.workspacesRoot}/*' -- "$directory"
            echo "workspace must be below ${settings.workspacesRoot}" >&2
            return 64
        end

        set -l password (sudo cat ${settings.secrets.serverPassword})
        set -l read_status $status
        if test $read_status -ne 0
            return $read_status
        end

        set -lx OPENCODE_SERVER_USERNAME opencode
        set -lx OPENCODE_SERVER_PASSWORD "$password"
        command opencode attach http://127.0.0.1:${toString settings.opencodePort} \
            --dir "$directory" $argv
      '';
      shellAbbrs.og = "sudo opencode-git";
    };
    git.settings.safe.directory = "${settings.workspacesRoot}/*";
  };
}
