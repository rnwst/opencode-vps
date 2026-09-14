import assert from "node:assert/strict"
import { randomUUID } from "node:crypto"
import { readFile } from "node:fs/promises"
import test from "node:test"
import { createContext, SourceTextModule, SyntheticModule } from "node:vm"

// Load the Nix output intact: only the SDK, filesystem effects, and Bun are mocked.
const source = await readFile(process.env.OPENCODE_PLUGIN, "utf8")
const directory = `${process.env.WORKSPACES_ROOT}/project`
const tmp = `${process.env.WORKSPACES_TMP_ROOT}/project`
const pr = "https://github.com/example/project/pull/42"
const session = (id, parentID) => ({ id, directory, ...(parentID === undefined ? {} : { parentID }) })
const family = () => [
  session("ses_root"),
  session("ses_child", "ses_root"),
  session("ses_grandchild", "ses_child"),
  session("ses_sibling", "ses_root"),
  session("ses_unrelated"),
]

async function harness({ sessions = family(), get, stopExit = Promise.resolve(0) } = {}) {
  const records = new Map(sessions.map((record) => [record.id, record]))
  const lookups = []
  const effects = []
  const requests = []
  const context = createContext({
    Bun: {
      resolveSync(name) {
        assert.equal(name, "@opencode-ai/plugin")
        return "mock:plugin"
      },
      sleep() { throw new Error("Unexpected managed-response polling") },
      spawn(command, options) {
        effects.push({ type: "stop", command: Array.from(command), options })
        return { exited: stopExit.then((status) => {
          effects.push({ type: "exit", status })
          return status
        }) }
      },
    },
  })
  const schema = {
    url() { return this },
    describe() { return this },
    regex() { return this },
    optional() { return this },
    max() { return this },
  }
  const tool = Object.assign((definition) => definition, {
    schema: { string: () => schema, enum: () => schema },
  })
  const modules = new Map()
  for (const [name, exports] of Object.entries({
    "node:crypto": { randomUUID },
    "node:fs": { realpathSync: (path) => path },
    "node:fs/promises": {
      async writeFile(path, payload, options) {
        effects.push({ type: "write", path, options })
        requests.push(JSON.parse(payload))
      },
      async rename(from, to) { effects.push({ type: "rename", from, to }) },
      async readFile(path) {
        const request = requests.find((item) => path.endsWith(`/response-${item.request_id}.json`))
        assert.ok(request, `Unexpected read: ${path}`)
        return JSON.stringify({ request_id: request.request_id, ok: true, result: { configured: true } })
      },
      async unlink(path) { effects.push({ type: "unlink", path }) },
      async rm(path, options) { effects.push({ type: "rm", path, options }) },
    },
    "mock:plugin": { tool },
  })) {
    const module = new SyntheticModule(Object.keys(exports), function () {
      for (const [key, value] of Object.entries(exports)) this.setExport(key, value)
    }, { context })
    await module.link(() => { throw new Error("Unexpected mock dependency") })
    await module.evaluate()
    modules.set(name, module)
  }
  const resolve = (name) => {
    assert.ok(modules.has(name), `Unexpected import: ${name}`)
    return modules.get(name)
  }
  const module = new SourceTextModule(source, {
    context,
    identifier: process.env.OPENCODE_PLUGIN,
    importModuleDynamically: resolve,
  })
  await module.link(resolve)
  await module.evaluate()
  const hooks = await module.namespace.ManagedHost({
    directory,
    client: { session: { async get(options) {
      assert.deepEqual(JSON.parse(JSON.stringify(options)), {
        path: { id: options.path.id }, query: { directory }, throwOnError: true,
      })
      lookups.push(options.path.id)
      return get ? get(options.path.id) : { data: records.get(options.path.id) }
    } } },
  })
  return { hooks, lookups, effects, requests }
}

const toolContext = (sessionID, cwd = directory) => ({
  sessionID, directory: cwd, abort: new AbortController().signal,
})
const deleted = (id) => ({ event: { type: "session.deleted", properties: { info: { id } } } })

test("shell and browser route related sessions to their root, without crossing conversations", async () => {
  const h = await harness()
  for (const [id, ancestry] of [
    ["ses_root", ["ses_root"]],
    ["ses_child", ["ses_child", "ses_root"]],
    ["ses_grandchild", ["ses_grandchild", "ses_child", "ses_root"]],
    ["ses_sibling", ["ses_sibling", "ses_root"]],
    ["ses_unrelated", ["ses_unrelated"]],
  ]) {
    const root = ancestry.at(-1)
    h.lookups.length = 0
    const shell = { env: { KEEP: "value" } }
    await h.hooks["shell.env"]({ sessionID: id }, shell)
    assert.deepEqual(shell.env, { KEEP: "value", OPENCODE_SESSION_ID: root })
    assert.deepEqual(h.lookups, ancestry)

    h.lookups.length = 0
    const browser = { args: { __opencode_session_id: "ses_forged", url: "https://example.com" } }
    await h.hooks["tool.execute.before"]({ tool: "playwright_browser_navigate", sessionID: id }, browser)
    assert.deepEqual(browser.args, { __opencode_session_id: root, url: "https://example.com" })
    assert.deepEqual(h.lookups, ancestry)
  }
  assert.deepEqual(h.effects, [])
})

test("sessionless shells remain supported without an SDK lookup", async () => {
  const h = await harness()
  const output = { env: {} }
  await h.hooks["shell.env"]({}, output)
  assert.equal(output.env.OPENCODE_SESSION_ID, undefined)
  assert.deepEqual(h.lookups, [])
})

test("both managed tools write the trusted root session and tracking names the conversation", async () => {
  const h = await harness()
  assert.equal(await h.hooks.tool.github_track_pr.execute({ pr_url: pr }, toolContext("ses_grandchild")),
    `Registered ${pr} with this conversation`)
  const args = { action: "fetch", remote: "origin" }
  assert.deepEqual(JSON.parse(await h.hooks.tool.github_manage_remote.execute(args, toolContext("ses_sibling"))),
    { configured: true })
  assert.deepEqual(h.requests.map(({ operation, session_id, directory: cwd, arguments: args }) =>
    ({ operation, session_id, directory: cwd, arguments: args })), [
    { operation: "track_pr", session_id: "ses_root", directory, arguments: { pr_url: pr } },
    { operation: "manage_github_remote", session_id: "ses_root", directory, arguments: args },
  ])
  assert.deepEqual(h.lookups, ["ses_grandchild", "ses_child", "ses_root", "ses_sibling", "ses_root"])
  assert.equal(h.effects.filter(({ type }) => type === "rename").length, 2)
})

test("untrusted or unavailable ancestry fails before routing or managed filesystem effects", async (t) => {
  const tooDeep = Array.from({ length: 65 }, (_, i) => session(`ses_n${i}`, i < 64 ? `ses_n${i + 1}` : undefined))
  for (const [name, id, options] of [
    ["malformed caller ID", "ses_bad/path", {}],
    ["missing session", "ses_missing", {}],
    ["SDK failure", "ses_child", { get: async () => { throw new Error("SDK unavailable") } }],
    ["missing ancestor", "ses_child", { sessions: [session("ses_child", "ses_missing")] }],
    ["cross-directory ancestor", "ses_child", { sessions: [session("ses_child", "ses_root"), { ...session("ses_root"), directory: `${directory}/` }] }],
    ["mismatched response ID", "ses_child", { get: () => ({ data: session("ses_other") }) }],
    ["malformed parent ID", "ses_child", { sessions: [session("ses_child", "not-a-session")] }],
    ["cyclic ancestry", "ses_child", { sessions: [session("ses_child", "ses_root"), session("ses_root", "ses_child")] }],
    ["ancestry beyond 64", "ses_n0", { sessions: tooDeep }],
  ]) {
    await t.test(name, async () => {
      const h = await harness(options)
      const shell = { env: {} }
      await assert.rejects(h.hooks["shell.env"]({ sessionID: id }, shell))
      assert.deepEqual(shell.env, {})
      const browser = { args: { __opencode_session_id: "ses_forged" } }
      await assert.rejects(h.hooks["tool.execute.before"]({ tool: "playwright_browser_snapshot", sessionID: id }, browser))
      assert.equal(browser.args.__opencode_session_id, "ses_forged")
      await assert.rejects(h.hooks.tool.github_track_pr.execute({ pr_url: pr }, toolContext(id)))
      assert.deepEqual(h.effects, [])
      assert.deepEqual(h.requests, [])
      assert.ok(h.lookups.length <= 3 * 64, "ancestry lookups must be bounded for each call")
    })
  }
})

test("managed tools reject mismatched context directories and missing sessions before writing", async () => {
  const h = await harness()
  for (const context of [toolContext("ses_child", `${directory}/`), toolContext(undefined)]) {
    await assert.rejects(h.hooks.tool.github_track_pr.execute({ pr_url: pr }, context))
    await assert.rejects(h.hooks.tool.github_manage_remote.execute({ action: "fetch" }, context))
  }
  assert.deepEqual(h.effects, [])
})

test("64-session ancestry is supported", async () => {
  const sessions = Array.from({ length: 64 }, (_, i) => session(`ses_n${i}`, i < 63 ? `ses_n${i + 1}` : undefined))
  const h = await harness({ sessions })
  const output = { env: {} }
  await h.hooks["shell.env"]({ sessionID: "ses_n0" }, output)
  assert.equal(output.env.OPENCODE_SESSION_ID, "ses_n63")
  assert.equal(h.lookups.length, 64)
})

test("deletion stops only the exact deleted session and waits for stop before removing its tmp", async () => {
  for (const id of ["ses_child", "ses_root"]) {
    let release
    const stopExit = new Promise((resolve) => { release = resolve })
    const h = await harness({ stopExit, get: () => { throw new Error("Deletion must not resolve ancestry") } })
    const deleting = h.hooks.event(deleted(id))
    assert.deepEqual(h.effects.map(({ type }) => type), ["stop"])
    assert.match(h.effects[0].command[0], /\/bin\/opencode-session-exec$/)
    assert.deepEqual(h.effects[0].command.slice(1), ["stop", "--session", id, "--directory", directory])
    release(0)
    await deleting
    assert.deepEqual(h.effects.map(({ type }) => type), ["stop", "exit", "rm"])
    assert.equal(h.effects[2].path, `${tmp}/${id}`)
    assert.deepEqual(JSON.parse(JSON.stringify(h.effects[2].options)), { recursive: true, force: true })
    assert.deepEqual(h.lookups, [])
  }
})

test("failed stop skips deletion, and malformed deleted IDs have no effects", async () => {
  const h = await harness({ stopExit: Promise.resolve(1) })
  await assert.rejects(h.hooks.event(deleted("ses_root")), /stop/i)
  assert.deepEqual(h.effects.map(({ type }) => type), ["stop", "exit"])
  h.effects.length = 0
  await h.hooks.event(deleted("ses_root/../ses_other"))
  assert.deepEqual(h.effects, [])
  assert.deepEqual(h.lookups, [])
})

test("direct file tools still reject /tmp but allow workspace and similarly named paths", async () => {
  const h = await harness()
  for (const [tool, key] of [["edit", "filePath"], ["glob", "path"], ["grep", "path"], ["read", "filePath"], ["write", "filePath"]]) {
    for (const path of ["/tmp", "/tmp/file"]) {
      await assert.rejects(h.hooks["tool.execute.before"]({ tool, sessionID: "ses_child" }, { args: { [key]: path } }), /temporary files/)
    }
    for (const path of [`${directory}/file`, "/tmpfile"]) {
      await h.hooks["tool.execute.before"]({ tool, sessionID: "ses_child" }, { args: { [key]: path } })
    }
  }
  assert.deepEqual(h.lookups, [])
  assert.deepEqual(h.effects, [])
})
