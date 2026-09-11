// SRT_PACKAGE is the built package root containing dist/ and node_modules/.
// Only the test process mocks DNS and socket dialing; no live service is used.
import assert from 'node:assert/strict'
import dns from 'node:dns'
import dnsPromises from 'node:dns/promises'
import http from 'node:http'
import https from 'node:https'
import net from 'node:net'
import tls from 'node:tls'
import { once } from 'node:events'
import { syncBuiltinESMExports } from 'node:module'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { test } from 'node:test'

assert.ok(process.env.SRT_PACKAGE, 'Set SRT_PACKAGE to the built SRT package root')
const moduleURL = name => pathToFileURL(resolve(
  process.env.SRT_PACKAGE, 'dist/sandbox', `${name}.js`,
)).href

const blocked = [
  '0.0.0.0', '0.1.2.3', '10.0.0.1', '100.64.0.1', '100.127.255.255',
  '127.0.0.1', '169.254.169.254', '172.16.0.1', '172.31.255.255',
  '192.0.0.9', '192.0.2.1', '192.88.99.1', '192.168.1.1',
  '198.18.0.1', '198.19.255.255', '198.51.100.1', '203.0.113.1',
  '224.0.0.1', '239.255.255.255', '240.0.0.1', '255.255.255.255',
  '::', '::1', '::127.0.0.1', '::ffff:127.0.0.1', '::ffff:8.8.8.8',
  '64:ff9b::7f00:1', '64:ff9b:1::a00:1', '100::1', '2001::1',
  '2001:2::1', '2001:20::1', '2001:db8::1', '2002:7f00:1::1',
  '3ffe::1', '3fff::1', '5f00::1', 'fc00::1', 'fd00::1', 'fe80::1', 'ff02::1',
]
const publicV4 = '93.184.216.34'
const publicV6 = '2606:4700:4700::1111'
const records = (...addresses) => addresses.map(address => ({
  address, family: net.isIP(address),
}))
const authority = host => net.isIP(host) === 6 ? `[${host}]` : host

test('built SRT enforces public-only destination dialing', { timeout: 60000 }, async t => {
  const originalConnect = net.Socket.prototype.connect
  const originalLookup = dns.lookup
  const sockets = new Set()
  const dials = []
  const lookups = []
  let answer = records(publicV4)
  let routePort
  const track = socket => {
    sockets.add(socket)
    socket.on('error', () => {})
    socket.once('close', () => sockets.delete(socket))
    return socket
  }
  t.after(() => {
    for (const socket of sockets) socket.destroy()
    t.mock.restoreAll()
    syncBuiltinESMExports()
  })
  t.mock.method(dnsPromises, 'lookup', async (host, options) => {
    lookups.push(host)
    assert.deepEqual(options, { all: true, verbatim: true })
    if (answer instanceof Error) throw answer
    return answer
  })
  t.mock.method(dns, 'lookup', (host, ...args) => {
    // Node's listen() calls lookup even for our numeric loopback bind address.
    if (host === '127.0.0.1') return originalLookup(host, ...args)
    assert.fail('Unexpected second DNS resolution while dialing')
  })
  t.mock.method(net.Socket.prototype, 'connect', function (...args) {
    const [first, second, third] = Array.isArray(args[0]) ? args[0] : args
    const options = typeof first === 'object'
      ? first : { port: first, host: typeof second === 'string' ? second : undefined }
    const callback = typeof second === 'function' ? second : third
    track(this)
    // The TLS terminator's internal transport is a Unix socket, not egress.
    if (options.path) return originalConnect.call(this, options, callback)
    dials.push({ host: options.host, port: Number(options.port) })
    if (![publicV4, publicV6].includes(options.host) || !routePort) {
      queueMicrotask(() => this.destroy(new Error('Test blocked unexpected dial')))
      return this
    }
    return originalConnect.call(this, {
      ...options, host: '127.0.0.1', port: routePort, family: 4,
    }, callback)
  })
  syncBuiltinESMExports()

  const { isPublicAddress, resolvePublicAddress, dialDirect, resolveParentProxy } =
    await import(moduleURL('parent-proxy'))
  const { createHttpProxyServer } = await import(moduleURL('http-proxy'))
  const { createSocksProxyServer } = await import(moduleURL('socks-proxy'))
  const { createMitmCA, disposeMitmCA } = await import(moduleURL('mitm-ca'))
  const { mintLeafCert } = await import(moduleURL('mitm-leaf'))

  const listen = async server => {
    server.on('connection', track)
    server.listen(0, '127.0.0.1')
    await once(server, 'listening')
    t.after(() => server.close())
    return server.address().port
  }
  const client = async port => {
    const socket = track(new net.Socket())
    originalConnect.call(socket, { host: '127.0.0.1', port })
    await once(socket, 'connect')
    return socket
  }
  const readUntil = (socket, complete) => new Promise((resolve, reject) => {
    let data = Buffer.alloc(0)
    const finish = error => {
      socket.off('data', onData)
      socket.off('error', onError)
      socket.off('end', onEnd)
      if (error) reject(error)
      else resolve(data)
    }
    const onData = chunk => {
      data = Buffer.concat([data, chunk])
      if (complete(data)) finish()
    }
    const onError = error => finish(error)
    const onEnd = () => finish()
    socket.on('data', onData)
    socket.once('error', onError)
    socket.once('end', onEnd)
  })
  const headers = socket => readUntil(socket, data => data.includes('\r\n\r\n'))
  const response = async (socket, request) => {
    const result = readUntil(socket, () => false)
    socket.write(request)
    const text = (await result).toString()
    socket.destroy()
    return text
  }
  const filter = port => port !== 22
  const proxyPort = await listen(createHttpProxyServer({ filter }))
  const socks = createSocksProxyServer({ filter })
  t.after(() => socks.close())
  const socksPort = await listen(net.createServer(s => socks.handleConnection(s)))
  const absolute = async host => response(await client(proxyPort),
    `GET http://${authority(host)}:8080/check HTTP/1.1\r\nHost: forged.invalid\r\nConnection: close\r\n\r\n`)
  const connect = async (host, port = 8080, proxy = proxyPort) => {
    const socket = await client(proxy)
    const result = headers(socket)
    socket.write(`CONNECT ${authority(host)}:${port} HTTP/1.1\r\nHost: ignored\r\n\r\n`)
    return { socket, reply: (await result).toString() }
  }
  const socksConnect = async (host, port = 8080, atyp = 3) => {
    const socket = await client(socksPort)
    const greeting = readUntil(socket, data => data.length >= 2)
    socket.write(Buffer.from([5, 1, 0]))
    assert.deepEqual(await greeting, Buffer.from([5, 0]))
    const address = atyp === 1 ? Buffer.from(host.split('.').map(Number))
      : atyp === 4 ? Buffer.from(host, 'hex')
        : Buffer.concat([Buffer.from([Buffer.byteLength(host)]), Buffer.from(host)])
    const portBytes = Buffer.alloc(2)
    portBytes.writeUInt16BE(port)
    const reply = readUntil(socket, data => data.length >= 10)
    socket.write(Buffer.concat([Buffer.from([5, 1, 0, atyp]), address, portBytes]))
    return { socket, reply: await reply }
  }

  await t.test('special-use, mapped, transition and scoped addresses are rejected', async () => {
    for (const address of [...blocked, 'fe80::1%lo', `${publicV6}%lo`, 'not-an-ip']) {
      assert.equal(isPublicAddress(address), false, address)
    }
    for (const address of [publicV4, publicV6, '100.63.255.255', '100.128.0.0',
      '172.15.255.255', '172.32.0.0', '192.0.1.1', '198.17.255.255', '198.20.0.0']) {
      assert.equal(isPublicAddress(address), true, address)
    }
    const count = lookups.length
    for (const host of [...blocked, '127.1', '2130706433', '0x7f000001', '0177.0.0.1',
      '[::1]', '127.0.0.1.', 'fe80::1%lo', 'host\0.invalid']) {
      await assert.rejects(resolvePublicAddress(host), undefined, host)
    }
    assert.equal(lookups.length, count, 'literals must not be sent to DNS')
    assert.equal(await resolvePublicAddress(publicV4), publicV4)
    assert.equal(await resolvePublicAddress(publicV6), publicV6)
  })

  await t.test('every DNS answer is checked; empty, failed and rebound answers fail closed', async () => {
    for (const address of blocked) {
      for (const set of [records(publicV4, address), records(address, publicV6)]) {
        answer = set
        await assert.rejects(resolvePublicAddress('mixed.test'))
      }
    }
    for (const value of [[], new Error('ENOTFOUND')]) {
      answer = value
      await assert.rejects(resolvePublicAddress('failed.test'))
    }
    answer = records(publicV4, publicV6)
    assert.equal(await resolvePublicAddress('Rebind.Test.'), publicV4)
    assert.equal(lookups.at(-1), 'rebind.test')
    answer = records('127.0.0.1')
    await assert.rejects(resolvePublicAddress('rebind.test'))
  })

  await t.test('HTTP absolute URI, opaque CONNECT and SOCKS block literals and mixed DNS without dialing', async () => {
    answer = records(publicV4, '127.0.0.1')
    const count = dials.length
    for (const host of [...blocked, '127.1', '2130706433', '0x7f000001', 'blocked.test']) {
      assert.match(await absolute(host), /^HTTP\/1\.1 5\d\d/, host)
      const tunnel = await connect(host)
      assert.match(tunnel.reply, /^HTTP\/1\.1 5\d\d/, host)
      tunnel.socket.destroy()
      const sock = await socksConnect(host)
      assert.notEqual(sock.reply[1], 0, host)
      sock.socket.destroy()
    }
    for (const [host, atyp] of [['127.0.0.1', 1], ['00000000000000000000ffff7f000001', 4]]) {
      const sock = await socksConnect(host, 8080, atyp)
      assert.notEqual(sock.reply[1], 0)
      sock.socket.destroy()
    }
    assert.equal(dials.length, count, 'rejected destinations must never reach a dial')
  })

  const seen = []
  const origin = http.createServer((req, res) => {
    seen.push({ host: req.headers.host, url: req.url })
    res.end('public-origin')
  })
  const originPort = await listen(origin)
  await t.test('all opaque/plaintext routes pin public answers and recheck on the next connection', async () => {
    routePort = originPort
    for (const route of ['http', 'connect', 'socks']) {
      for (const address of [publicV4, publicV6]) {
        answer = records(address)
        const count = lookups.length
        let text
        if (route === 'http') text = await absolute('public.test')
        else {
          const tunnel = route === 'connect'
            ? await connect('public.test') : await socksConnect('public.test')
          if (route === 'connect') assert.match(tunnel.reply, /^HTTP\/1\.1 200/)
          else assert.equal(tunnel.reply[1], 0)
          text = await response(tunnel.socket,
            'GET /check HTTP/1.1\r\nHost: public.test:8080\r\nConnection: close\r\n\r\n')
        }
        assert.match(text, /public-origin/)
        assert.equal(lookups.length, count + 1)
        assert.deepEqual(dials.at(-1), { host: address, port: 8080 })
        assert.deepEqual(seen.at(-1), { host: 'public.test:8080', url: '/check' })
      }
      answer = records('127.0.0.1')
      const count = dials.length
      if (route === 'http') assert.match(await absolute('public.test'), /^HTTP\/1\.1 500/)
      else {
        const tunnel = route === 'connect'
          ? await connect('public.test') : await socksConnect('public.test')
        if (route === 'connect') assert.match(tunnel.reply, /^HTTP\/1\.1 502/)
        else assert.notEqual(tunnel.reply[1], 0)
        tunnel.socket.destroy()
      }
      assert.equal(dials.length, count)
    }
    answer = records(publicV4, publicV6)
    const literalLookups = lookups.length
    assert.match(await absolute(publicV4), /public-origin/)
    const literal = await socksConnect(publicV4, 8080, 1)
    assert.equal(literal.reply[1], 0)
    literal.socket.destroy()
    assert.equal(lookups.length, literalLookups, 'public literals dial without DNS')
    routePort = undefined
    const count = dials.length
    await assert.rejects(dialDirect('unreachable.test', 8080))
    assert.equal(dials.length, count + 1, 'no fallback dial after connection failure')
  })

  await t.test('shutdown closes half-open CONNECT sockets', { timeout: 3000 }, async () => {
    let upstream
    routePort = await listen(net.createServer({ allowHalfOpen: true }, socket => {
      upstream = socket
      socket.on('data', () => {})
    }))
    answer = records(publicV4)
    const proxy = createHttpProxyServer({ filter })
    const port = await listen(proxy)
    const tunnel = await connect('public.test', 8080, port)
    assert.match(tunnel.reply, /^HTTP\/1\.1 200/)
    const eof = once(upstream, 'end')
    tunnel.socket.end()
    await eof
    await new Promise(resolve => {
      proxy.closeAllConnections()
      proxy.close(resolve)
    })
  })

  await t.test('TLS-terminated upstream checks DNS per request and preserves Host, SNI and certificate verification', async () => {
    const ca = createMitmCA({})
    t.after(() => disposeMitmCA(ca))
    const leaf = mintLeafCert(ca, 'public.test')
    const tlsSeen = []
    routePort = await listen(https.createServer({ key: leaf.keyPem, cert: leaf.certPem }, (req, res) => {
      tlsSeen.push({ host: req.headers.host, sni: req.socket.servername })
      res.end('tls-public-origin')
    }))
    const terminatedPort = await listen(createHttpProxyServer({
      filter, mitmCA: ca, tlsTerminateUpstreamCA: ca.certPem,
    }))
    // Absolute HTTPS has no upstream-CA option. Inject only this test CA at
    // the request boundary while retaining Node's real TLS verification.
    const originalRequest = https.request
    const requestMock = t.mock.method(https, 'request', (options, callback) =>
      originalRequest({ ...options, ca: ca.certPem }, callback))
    syncBuiltinESMExports()
    try {
      answer = records(publicV6)
      const count = lookups.length
      assert.match(await response(await client(proxyPort),
        'GET https://public.test:8443/check HTTP/1.1\r\nHost: forged.invalid\r\nConnection: close\r\n\r\n'), /tls-public-origin/)
      assert.equal(lookups.length, count + 1)
      assert.deepEqual(dials.at(-1), { host: publicV6, port: 8443 })
      assert.deepEqual(tlsSeen.pop(), { host: 'public.test:8443', sni: 'public.test' })
      answer = records(publicV4, '127.0.0.1')
      const before = dials.length
      for (const host of ['public.test', '127.0.0.1', '[::ffff:127.0.0.1]']) {
        assert.match(await response(await client(proxyPort),
          `GET https://${host}:8443/ HTTP/1.1\r\nHost: ignored\r\nConnection: close\r\n\r\n`), /^HTTP\/1\.1 500/)
      }
      assert.equal(dials.length, before)
    } finally {
      requestMock.mock.restore()
      syncBuiltinESMExports()
    }
    const tunnel = await connect('public.test', 8443, terminatedPort)
    assert.match(tunnel.reply, /^HTTP\/1\.1 200/)
    const secure = track(tls.connect({ socket: tunnel.socket, servername: 'public.test', ca: ca.certPem }))
    await once(secure, 'secureConnect')
    answer = records(publicV4)
    const count = lookups.length
    const first = readUntil(secure, data => data.includes('tls-public-origin'))
    secure.write('GET / HTTP/1.1\r\nHost: forged.invalid\r\n\r\n')
    assert.match((await first).toString(), /^HTTP\/1\.1 200/)
    assert.equal(lookups.length, count + 1)
    assert.deepEqual(dials.at(-1), { host: publicV4, port: 8443 })
    assert.deepEqual(tlsSeen, [{ host: 'public.test:8443', sni: 'public.test' }])
    answer = records(publicV4, '127.0.0.1')
    const dialCount = dials.length
    assert.match(await response(secure,
      'GET / HTTP/1.1\r\nHost: public.test\r\nConnection: close\r\n\r\n'), /^HTTP\/1\.1 403/)
    assert.equal(dials.length, dialCount)
    assert.equal(lookups.length, count + 2, 'same tunnel must not cache DNS')

    for (const host of ['127.0.0.1', '::ffff:7f00:1', 'wrong.test']) {
      const tunnel = await connect(host, 8443, terminatedPort)
      // A public SNI must not override a private CONNECT destination.
      const secure = track(tls.connect({
        socket: tunnel.socket, servername: net.isIP(host) ? 'public.test' : host, ca: ca.certPem,
      }))
      await once(secure, 'secureConnect')
      answer = records(publicV4)
      const before = dials.length
      const text = await response(secure,
        'GET / HTTP/1.1\r\nHost: public.test\r\nConnection: close\r\n\r\n')
      assert.match(text, host === 'wrong.test' ? /^HTTP\/1\.1 502/ : /^HTTP\/1\.1 403/)
      assert.equal(dials.length, before + (host === 'wrong.test' ? 1 : 0))
    }
    assert.equal(tlsSeen.length, 1, 'bad certificates must not receive HTTP bytes')
  })

  await t.test('parent/env and external MITM proxy routes cannot bypass checks; explicit port deny remains first', async () => {
    const envKeys = ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'NO_PROXY', 'no_proxy']
    const saved = Object.fromEntries(envKeys.map(key => [key, process.env[key]]))
    try {
      for (const key of envKeys) delete process.env[key]
      assert.equal(resolveParentProxy(), undefined)
      for (const key of envKeys.slice(0, 4)) {
        process.env[key] = 'http://127.0.0.1:9999'
        process.env.NO_PROXY = '*'
        assert.throws(() => resolveParentProxy({ http: '', https: '' }), /unsupported/)
        delete process.env[key]
      }
      for (const cfg of [{ http: 'http://public.test' }, { https: 'invalid', noProxy: '*' }]) {
        assert.throws(() => resolveParentProxy(cfg), /unsupported/)
      }
    } finally {
      for (const [key, value] of Object.entries(saved)) {
        if (value === undefined) delete process.env[key]
        else process.env[key] = value
      }
    }
    assert.throws(() => createHttpProxyServer({ filter, parentProxy: {} }), /unsupported/)
    assert.throws(() => createSocksProxyServer({ filter, parentProxy: {} }), /unsupported/)
    const externalPort = await listen(createHttpProxyServer({
      filter, getMitmSocketPath: () => '/nonexistent-srt-test.sock',
    }))
    const count = dials.length
    const lookupCount = lookups.length
    const external = await connect('public.test', 8080, externalPort)
    assert.match(external.reply, /^HTTP\/1\.1 500/)
    external.socket.destroy()
    assert.match(await response(await client(externalPort),
      'GET http://public.test/ HTTP/1.1\r\nHost: ignored\r\nConnection: close\r\n\r\n'), /^HTTP\/1\.1 500/)
    const denied = await connect('public.test', 22)
    assert.match(denied.reply, /^HTTP\/1\.1 403/)
    denied.socket.destroy()
    const sock = await socksConnect('public.test', 22)
    assert.notEqual(sock.reply[1], 0)
    sock.socket.destroy()
    assert.equal(dials.length, count)
    assert.equal(lookups.length, lookupCount)
  })
})
