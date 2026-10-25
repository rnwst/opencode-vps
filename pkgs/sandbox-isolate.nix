{ pkgs, sandboxRuntime }:
let
  helper = "${sandboxRuntime}/lib/node_modules/@anthropic-ai/sandbox-runtime/vendor/seccomp/${
    if pkgs.stdenv.hostPlatform.isx86_64 then "x64" else "arm64"
  }/apply-seccomp";
in
pkgs.writeShellApplication {
  name = "opencode-sandbox-isolate";
  runtimeInputs = [ pkgs.coreutils ];
  text = ''
    # SRT invokes this trusted entry point after starting its socat relays.
    # A second bwrap replaces the inherited host root with an allowlist; its
    # recursive workspace binds retain SRT's read-only .git mounts. The native
    # helper then separates the workload from both bwrap and the relay tree.
    root="''${OPENCODE_SANDBOX_WORKSPACE:?missing sandbox workspace}"
    [[ "$root" == /* && "$root" != / && -d "$root" ]] || exit 77
    [[ "$HOME" == /* && "$HOME" != / ]] || exit 77
    bundle="''${SSL_CERT_FILE:?missing sandbox trust bundle}"
    [[ -f "$bundle" && ! -L "$bundle" ]] || exit 77
    args=(
      --die-with-parent --unshare-user --cap-drop ALL
      --ro-bind /nix/store /nix/store
      --ro-bind-try /sys /sys
      --dev /dev --proc /proc
      --bind /tmp /tmp
      --bind "$root" "$root"
      --dir "$HOME"
      --ro-bind "$bundle" "$bundle"
    )

    # Bind individual configuration files, never host runtime directories.
    # Read-only directory mounts alone do not prevent connecting to sockets.
    for path in \
      /bin/sh /usr/bin/env \
      /lib64/ld-linux-x86-64.so.2 /lib/ld-linux-aarch64.so.1 \
      /etc/passwd /etc/group /etc/nsswitch.conf /etc/hosts /etc/resolv.conf \
      /etc/protocols /etc/services /etc/gai.conf /etc/localtime /etc/os-release \
      /etc/ld.so.cache /etc/nix/nix.conf \
      "$HOME/.gitconfig" "$HOME/.config/git/config"; do
      if [[ -f "$path" ]]; then
        args+=(--ro-bind "$path" "$path")
      fi
    done

    # These NixOS directories must resolve to immutable store trees. In
    # particular, never expose all of /run, /etc or /nix/var/nix/daemon-socket.
    for path in /run/current-system /etc/fonts /etc/ssl /etc/terminfo \
      "/etc/profiles/per-user/$(basename "$HOME")"; do
      if [[ -e "$path" ]]; then
        resolved="$(realpath -e "$path")"
        if [[ "$path" == /etc/* && "$resolved" != /nix/store/* && -e "/etc/static/''${path#/etc/}" ]]; then
          resolved="$(realpath -e "/etc/static/''${path#/etc/}")"
        fi
        [[ "$resolved" == /nix/store/* ]] || {
          echo "sandbox runtime path is not immutable: $path" >&2; exit 77;
        }
        args+=(--ro-bind "$resolved" "$path")
      fi
    done

    # bwrap closes non-stdio inherited descriptors. The relays keep their own
    # mount view and can open broker sockets for every subsequent connection.
    # No network unshare here: retain the session loopback and proxy listeners.
    exec ${pkgs.bubblewrap}/bin/bwrap "''${args[@]}" \
      --remount-ro / --chdir "$PWD" \
      --unsetenv OPENCODE_SANDBOX_WORKSPACE -- \
      ${helper} --allow-unix-sockets "$@"
  '';
}
