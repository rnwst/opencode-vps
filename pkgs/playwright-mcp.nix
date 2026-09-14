{ pkgs }:
let
  # Reuse the browser revision already in playwright-mcp's closure.
  shells = pkgs.lib.filterAttrs (
    name: _: pkgs.lib.hasPrefix "chromium_headless_shell-" name
  ) pkgs.playwright-driver.browsers.entries;
  headlessShell = "${builtins.head (builtins.attrValues shells)}/chrome-headless-shell-linux64/chrome-headless-shell";
  launcher = pkgs.writeText "opencode-playwright-mcp.cjs" ''
    const fs = require('node:fs');
    const path = require('node:path');
    const { X509Certificate } = require('node:crypto');
    const { spawn, execFileSync } = require('node:child_process');

    const root = process.env.HOME;
    // The backend owns this directory and removes it after namespace teardown,
    // including cancellation by SIGKILL. Never remove it from this process.
    for (const dir of [process.env.TMPDIR, process.env.XDG_CONFIG_HOME,
      process.env.XDG_CACHE_HOME, process.env.XDG_DATA_HOME,
      process.env.XDG_STATE_HOME, process.env.XDG_RUNTIME_DIR]) {
      fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    }

    const proxy = new URL(process.env.HTTP_PROXY);
    if (proxy.protocol !== 'http:' ||
        !['127.0.0.1', 'localhost', '[::1]'].includes(proxy.hostname) ||
        !proxy.port || proxy.pathname !== '/' || proxy.search || proxy.hash) {
      throw new Error('HTTP_PROXY must be the session-local SRT HTTP proxy');
    }

    // Chromium does not use Node/OpenSSL's CA environment variables. Import
    // only CA certificates from SRT's public bundle, never its private key.
    const database = path.join(root, '.pki', 'nssdb');
    fs.mkdirSync(database, { recursive: true, mode: 0o700 });
    execFileSync('${pkgs.nssTools}/bin/certutil',
      ['-N', '-d', 'sql:' + database, '--empty-password']);
    const certificates = new Set();
    for (const bundle of new Set([
      process.env.SSL_CERT_FILE, process.env.NODE_EXTRA_CA_CERTS,
    ].filter(Boolean))) {
      const pem = fs.readFileSync(bundle, 'utf8');
      const blocks = pem.match(/-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----/g);
      if (!blocks) throw new Error('SRT trust bundle contains no certificates');
      for (const block of blocks) {
        const cert = new X509Certificate(block);
        if (!cert.ca || certificates.has(cert.fingerprint256)) continue;
        certificates.add(cert.fingerprint256);
        execFileSync('${pkgs.nssTools}/bin/certutil', [
          '-A', '-d', 'sql:' + database, '-n', cert.fingerprint256,
          '-t', 'C,,', '-a',
        ], { input: block });
      }
    }
    if (!certificates.size) throw new Error('SRT CA trust is unavailable');

    // Playwright strips URL userinfo; proxy authentication needs separate fields.
    const config = path.join(root, 'mcp.json');
    fs.writeFileSync(config, JSON.stringify({
      browser: { launchOptions: { proxy: {
        server: proxy.origin,
        username: decodeURIComponent(proxy.username),
        password: decodeURIComponent(proxy.password),
        // Remove Chromium's implicit 127/8 and link-local bypass first.
        bypass: '<-loopback>,127.0.0.1,localhost,[::1]',
      } } },
    }), { mode: 0o600 });
    const child = spawn('${pkgs.playwright-mcp}/bin/playwright-mcp', [
      '--config', config,
      '--headless', '--isolated', '--browser', 'chromium',
      '--executable-path', '${headlessShell}',
      // The enclosing session bwrap/seccomp sandbox remains mandatory.
      '--no-sandbox', '--output-dir', path.join(root, 'output'),
    ], { stdio: 'inherit' });
    for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP'])
      process.on(signal, () => child.kill(signal));
    child.on('error', error => { console.error(error); process.exit(1); });
    child.on('exit', (code, signal) => process.exit(code ?? (signal ? 1 : 0)));
  '';
in
assert pkgs.stdenv.hostPlatform.isx86_64 && pkgs.stdenv.hostPlatform.isLinux;
assert builtins.length (builtins.attrNames shells) == 1;
pkgs.writeShellApplication {
  name = "opencode-playwright-mcp";
  runtimeInputs = [ pkgs.coreutils ];
  text = ''
    [[ $# -eq 0 ]] || { echo "opencode-playwright-mcp accepts no arguments" >&2; exit 64; }
    [[ "''${SANDBOX_RUNTIME:-}" == 1 && -n "''${HTTP_PROXY:-}" ]] || {
      echo "opencode-playwright-mcp must run inside the session SRT sandbox" >&2
      exit 77
    }
    umask 077
    private_home="''${OPENCODE_PLAYWRIGHT_HOME:-}"
    [[ "$private_home" == /tmp/?* && -d "$private_home" && ! -L "$private_home" && -O "$private_home" \
       && "$private_home" == "$(realpath -e -- "$private_home")" \
       && "$(stat -c %a -- "$private_home")" == 700 ]] || {
      echo "OPENCODE_PLAYWRIGHT_HOME must be an owned canonical mode-0700 directory below /tmp" >&2
      exit 77
    }
    # An allowlisted environment also removes NODE_OPTIONS, MCP overrides,
    # host credentials, desktop sockets and host browser/profile settings.
    # A mandatory PID namespace contains Playwright's detached process groups.
    # Only /proc is remounted; inherited SRT mounts (including .git protections),
    # network namespace and seccomp remain intact. Parent death kills PID 1,
    # which makes the kernel terminate every browser descendant.
    exec env -i \
      HOME="$private_home" \
      TMPDIR="$private_home/tmp" \
      XDG_CONFIG_HOME="$private_home/.config" \
      XDG_CACHE_HOME="$private_home/.cache" \
      XDG_DATA_HOME="$private_home/.local/share" \
      XDG_STATE_HOME="$private_home/.local/state" \
      XDG_RUNTIME_DIR="$private_home/run" \
      PATH="${pkgs.lib.makeBinPath [ pkgs.coreutils ]}" \
      LANG=C.UTF-8 \
      HTTP_PROXY="$HTTP_PROXY" \
      NO_PROXY='127.0.0.1,localhost,[::1]' \
      SSL_CERT_FILE="''${SSL_CERT_FILE:-}" \
      NODE_EXTRA_CA_CERTS="''${NODE_EXTRA_CA_CERTS:-}" \
      PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
      FONTCONFIG_FILE=${pkgs.makeFontsConf { fontDirectories = [ pkgs.dejavu_fonts ]; }} \
      ${pkgs.util-linux}/bin/unshare \
        --user --map-current-user --pid --fork --kill-child=SIGKILL --mount-proc -- \
      ${pkgs.nodejs}/bin/node ${launcher}
  '';
}
