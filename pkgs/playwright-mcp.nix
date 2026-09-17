{ pkgs }:
let
  # Reuse the browser revision already in playwright-mcp's closure.
  shells = pkgs.lib.filterAttrs (
    name: _: pkgs.lib.hasPrefix "chromium_headless_shell-" name
  ) pkgs.playwright-driver.browsers.entries;
  headlessShell = "${builtins.head (builtins.attrValues shells)}/chrome-headless-shell-linux64/chrome-headless-shell";
  # WebKit's release build ignores WEBKIT_TLS_CAFILE_PEM. Give its GIO TLS
  # backend the session CA bundle, while retaining chain and hostname checks.
  webkit = pkgs.playwright-driver.components.webkit.override {
    glib-networking = pkgs.glib-networking.overrideAttrs (old: {
      postPatch = (old.postPatch or "") + ''
        substituteInPlace tls/gnutls/gtlsdatabase-gnutls.c \
          --replace-fail \
            'int gerr = gnutls_x509_trust_list_add_system_trust (trust_list, 0, 0);' \
            'const char *bundle = g_getenv ("SSL_CERT_FILE");
             int gerr = bundle ? gnutls_x509_trust_list_add_trust_file (trust_list, bundle, NULL, GNUTLS_X509_FMT_PEM, 0, 0) : gnutls_x509_trust_list_add_system_trust (trust_list, 0, 0);'
      '';
    });
  };
  launcher = pkgs.writeText "opencode-playwright-mcp.cjs" ''
    const fs = require('node:fs');
    const path = require('node:path');
    const { X509Certificate } = require('node:crypto');
    const { spawn, execFileSync } = require('node:child_process');
    const http = require('node:http');
    const net = require('node:net');

    (async () => {
    const root = process.env.HOME;
    const browser = process.argv[2];
    if (browser === 'firefox') {
      // Use Mesa's headless EGL path instead of GLX, which needs an X display.
      // Firefox dlopens the EGL dispatch library, so provide its pinned path.
      process.env.MOZ_WEBGL_FORCE_EGL = '1';
      process.env.LD_LIBRARY_PATH = '${pkgs.lib.makeLibraryPath [ pkgs.libglvnd ]}';
    }
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

    // Chromium and Firefox do not use Node/OpenSSL's CA variables. Import
    // only CA certificates from SRT's public bundle, never its private key.
    const database = browser === 'firefox' ? path.join(root, 'firefox-profile') : path.join(root, '.pki', 'nssdb');
    fs.mkdirSync(database, { recursive: true, mode: 0o700 });
    execFileSync('${pkgs.nssTools}/bin/certutil',
      ['-N', '-d', 'sql:' + database, '--empty-password']);
    const certificates = new Set();
    const trustedPem = [];
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
        trustedPem.push(block);
        execFileSync('${pkgs.nssTools}/bin/certutil', [
          '-A', '-d', 'sql:' + database, '-n', cert.fingerprint256,
          '-t', 'C,,', '-a',
        ], { input: block });
      }
    }
    if (!certificates.size) throw new Error('SRT CA trust is unavailable');
    if (browser === 'webkit') {
      const bundle = path.join(root, 'trust.pem');
      fs.writeFileSync(bundle, trustedPem.join('\n'), { mode: 0o600 });
      process.env.SSL_CERT_FILE = bundle;
    }

    let browserProxy = {
      server: proxy.origin,
      username: decodeURIComponent(proxy.username),
      password: decodeURIComponent(proxy.password),
      bypass: '<-loopback>,127.0.0.1,localhost,[::1]',
    };
    if (browser !== 'chromium') {
      // Firefox and WebKit treat bypass hostnames as suffixes. Route exact
      // loopback here instead, and keep SRT credentials out of WebKit headers.
      const authorization = 'Basic ' + Buffer.from(
        browserProxy.username + ':' + browserProxy.password).toString('base64');
      const relay = http.createServer();
      relay.maxConnections = 128;
      const forward = (request, response, head) => {
        const tunnel = request.method === 'CONNECT';
        let target;
        try {
          target = new URL(tunnel ? 'http://' + request.url : request.url);
          if (target.protocol === 'ws:') target.protocol = 'http:';
          if (target.protocol !== 'http:' || target.username || target.password)
            throw new Error('invalid proxy target');
        } catch {
          response.destroy();
          return;
        }
        const local = ['127.0.0.1', 'localhost', '[::1]'].includes(target.hostname);
        const host = local ? (target.hostname === '[::1]' ? '::1' : '127.0.0.1') : proxy.hostname.replace(/^\[|\]$/g, "");
        const port = local ? (target.port || 80) : proxy.port;
        const headers = { ...request.headers };
        delete headers['proxy-authorization'];
        delete headers['proxy-connection'];
        if (!local) headers['proxy-authorization'] = authorization;
        const splice = (socket, buffered = Buffer.alloc(0)) => {
          socket.on('error', () => response.destroy());
          response.on('error', () => socket.destroy());
          response.on('close', () => socket.destroy());
          socket.on('close', () => response.destroy());
          if (buffered.length) response.write(buffered);
          if (head?.length) socket.write(head);
          response.pipe(socket).pipe(response);
        };
        if (tunnel && local) {
          const socket = net.connect({ host, port });
          socket.on('error', () => response.destroy());
          response.on('close', () => socket.destroy());
          socket.once('connect', () => {
            response.write('HTTP/1.1 200 Connection Established\r\n\r\n');
            splice(socket);
          });
          return;
        }
        const outgoing = http.request({
          host, port, method: request.method, headers, agent: false,
          path: tunnel ? request.url : local ? target.pathname + target.search : target.href,
        });
        outgoing.on('error', () => response.destroy());
        response.on('close', () => outgoing.destroy());
        outgoing.on('connect', (incoming, socket, buffered) => {
          if (incoming.statusCode !== 200) {
            socket.destroy();
            response.destroy();
            return;
          }
          response.write('HTTP/1.1 200 Connection Established\r\n\r\n');
          splice(socket, buffered);
        });
        outgoing.on('upgrade', (incoming, socket, buffered) => {
          response.write('HTTP/1.1 101 Switching Protocols\r\n' +
            Object.entries(incoming.headers).map(([k, v]) => k + ': ' + v).join('\r\n') + '\r\n\r\n');
          splice(socket, buffered);
        });
        outgoing.on('response', incoming => {
          if (head !== undefined) {
            incoming.destroy();
            response.destroy();
            return;
          }
          response.writeHead(incoming.statusCode, incoming.headers);
          incoming.on('error', () => response.destroy());
          incoming.pipe(response);
        });
        request.on('error', () => outgoing.destroy());
        if (head !== undefined) outgoing.end();
        else request.pipe(outgoing);
      };
      relay.on('request', forward);
      relay.on('connect', forward);
      relay.on('upgrade', forward);
      await new Promise((resolve, reject) => {
        relay.once('error', reject);
        relay.listen(0, '127.0.0.1', resolve);
      });
      browserProxy = { server: 'http://127.0.0.1:' + relay.address().port };
    }

    // Playwright strips URL userinfo; proxy authentication needs separate fields.
    const config = path.join(root, 'mcp.json');
    fs.writeFileSync(config, JSON.stringify({
      browser: { launchOptions: {
        proxy: browserProxy,
        ...(browser === 'firefox' ? { firefoxUserPrefs: {
          // Override the headless GPU blocklist; LIBGL_ALWAYS_SOFTWARE still applies.
          'webgl.force-enabled': true,
        } } : {}),
      } },
    }), { mode: 0o600 });
    const child = spawn('${pkgs.playwright-mcp}/bin/playwright-mcp', [
      '--config', config,
      '--headless', '--browser', browser,
      ...(browser === 'firefox' ? ['--user-data-dir', database] : ['--isolated']),
      ...(browser === 'chromium' ? ['--executable-path', '${headlessShell}'] : []),
      ...(browser === 'webkit' ? ['--executable-path', '${webkit}/pw_run.sh'] : []),
      // The enclosing session bwrap/seccomp sandbox remains mandatory.
      '--no-sandbox', '--output-dir', path.join(root, 'output'),
    ], { stdio: 'inherit' });
    for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP'])
      process.on(signal, () => child.kill(signal));
    child.on('error', error => { console.error(error); process.exit(1); });
    child.on('exit', (code, signal) => process.exit(code ?? (signal ? 1 : 0)));
    })().catch(error => { console.error(error); process.exit(1); });
  '';
in
assert pkgs.stdenv.hostPlatform.isx86_64 && pkgs.stdenv.hostPlatform.isLinux;
assert builtins.length (builtins.attrNames shells) == 1;
pkgs.writeShellApplication {
  name = "opencode-playwright-mcp";
  runtimeInputs = [ pkgs.coreutils ];
  text = ''
    [[ $# -le 1 ]] || { echo "expected at most one browser name" >&2; exit 64; }
    browser="''${1-chromium}"
    case "$browser" in
      chromium|firefox|webkit) ;;
      *) echo "expected chromium, firefox or webkit" >&2; exit 64 ;;
    esac
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
      LIBGL_ALWAYS_SOFTWARE=1 \
      __EGL_VENDOR_LIBRARY_FILENAMES=${pkgs.mesa}/share/glvnd/egl_vendor.d/50_mesa.json \
      FONTCONFIG_FILE=${pkgs.makeFontsConf { fontDirectories = [ pkgs.dejavu_fonts ]; }} \
      ${pkgs.util-linux}/bin/unshare \
        --user --map-current-user --pid --fork --kill-child=SIGKILL --mount-proc -- \
      ${pkgs.nodejs}/bin/node ${launcher} "$browser"
  '';
}
