import assert from "node:assert/strict"
import { randomUUID } from "node:crypto"
import { readFile } from "node:fs/promises"
import test from "node:test"
import { createContext, SourceTextModule, SyntheticModule } from "node:vm"

const source = await readFile(process.env.OPENCODE_PLUGIN, "utf8")
const directory = `${process.env.WORKSPACES_ROOT}/project`
const tmp = `${process.env.WORKSPACES_TMP_ROOT}/project`
const session = (id, parentID, dir = directory) => ({ id, location: { directory: dir }, parentID })
const family = () => [session("sesRoot"), session("sesChild", "sesRoot"), session("sesOther"), session("ses_legacy")]
const tick = () => new Promise(setImmediate)
const contextFor = sessionID => ({ sessionID, signal: new AbortController().signal })

async function harness({ sessions = family(), get, stopExit = Promise.resolve(0) } = {}) {
  const records = new Map(sessions.map(record => [record.id, record]))
  const lookups = [], effects = [], requests = [], errors = []
  const hooks = {}, tools = new Map()
  let receive
  const context = createContext({
    AbortController,
    console: { error: (...args) => errors.push(args) },
    Bun: {
      sleep() { throw new Error("Unexpected response polling") },
      spawn(command) {
        effects.push({ type: "stop", command: Array.from(command) })
        return { exited: stopExit.then(status => { effects.push({ type: "exit", status }); return status }) }
      },
    },
  })
  const modules = new Map()
  for (const [name, exports] of Object.entries({
    "node:crypto": { randomUUID },
    "node:fs": { realpathSync: path => path },
    "node:fs/promises": {
      async writeFile(path, payload) { effects.push({ type: "write", path }); requests.push(JSON.parse(payload)) },
      async rename(from, to) { effects.push({ type: "rename", from, to }) },
      async readFile(path) {
        const request = requests.find(item => path.endsWith(`/response-${item.request_id}.json`))
        assert.ok(request)
        return JSON.stringify({ request_id: request.request_id, ok: true, result: { configured: true } })
      },
      async unlink() {},
      async rm(path) { effects.push({ type: "rm", path }) },
    },
  })) {
    const module = new SyntheticModule(Object.keys(exports), function () {
      for (const [key, value] of Object.entries(exports)) this.setExport(key, value)
    }, { context })
    await module.link(() => { throw new Error("Unexpected dependency") })
    await module.evaluate()
    modules.set(name, module)
  }
  const module = new SourceTextModule(source, { context })
  await module.link(name => { assert.ok(modules.has(name), name); return modules.get(name) })
  await module.evaluate()
  const shell = { name: "shell", async execute(input, ctx) {
    const event = { sessionID: ctx.sessionID, env: { OPENCODE_MANAGED_SESSION_ID: "sesForged" }, shell: "/bin/sh" }
    await hooks.shell(event)
    // V2 Core writes this after shell.create.before.
    event.env.OPENCODE_SESSION_ID = ctx.sessionID
    return event
  } }
  tools.set("shell", shell)
  const cleanup = await module.namespace.default.setup({
    location: { directory },
    session: { async get({ sessionID }) {
      lookups.push(sessionID)
      return get ? get(sessionID) : records.get(sessionID)
    } },
    event: { subscribe({ signal }) {
      return { [Symbol.asyncIterator]() { return this }, next() {
        return new Promise(resolve => {
          receive = value => resolve({ value, done: false })
          signal.addEventListener("abort", () => resolve({ done: true }), { once: true })
        })
      } }
    } },
    shell: { async hook(name, callback) { assert.equal(name, "create.before"); hooks.shell = callback } },
    tool: {
      async hook(name, callback) { assert.equal(name, "execute.before"); hooks.before = callback },
      async transform(callback) { callback({
        add(definition) { tools.set(definition.name, definition) },
        update(name, update) { update(tools.get(name)) },
      }) },
    },
  })
  return { hooks, tools, lookups, effects, requests, errors, cleanup,
    async deleted(id, dir = directory) {
      receive({ type: "session.deleted", ...(dir === null ? {} : { location: { directory: dir } }), data: { sessionID: id } })
      await tick()
    },
  }
}

test("V2 shell calls resolve trusted root ancestry, including legacy IDs", async () => {
  const h = await harness()
  for (const [id, root] of [["sesRoot", "sesRoot"], ["sesChild", "sesRoot"], ["sesOther", "sesOther"], ["ses_legacy", "ses_legacy"]]) {
    const result = await h.tools.get("shell").execute({}, contextFor(id))
    assert.equal(result.env.OPENCODE_MANAGED_SESSION_ID, root)
    assert.match(result.shell, /\/bin\/opencode-session-exec$/)
  }
  h.cleanup()
})

test("sessionless shells discard forged managed routing", async () => {
  const h = await harness()
  const event = { env: { OPENCODE_MANAGED_SESSION_ID: "sesForged" } }
  await h.hooks.shell(event)
  assert.equal(event.env.OPENCODE_MANAGED_SESSION_ID, undefined)
  assert.deepEqual(h.lookups, [])
  h.cleanup()
})

test("managed tools use root session IDs and return V2 content", async () => {
  const h = await harness()
  const pr = "https://github.com/example/project/pull/42"
  const result = await h.tools.get("github_track_pr").execute({ pr_url: pr }, contextFor("sesChild"))
  assert.equal(result.content, `Registered ${pr} with this conversation`)
  const remote = await h.tools.get("github_manage_remote").execute({ action: "fetch" }, contextFor("sesChild"))
  assert.deepEqual(JSON.parse(remote.content), { configured: true })
  assert.ok(h.requests.every(request => request.session_id === "sesRoot" && request.directory === directory))
  h.cleanup()
})

test("invalid, cyclic and cross-directory ancestry fails before effects", async () => {
  for (const options of [
    { get: () => undefined },
    { get: () => session("sesOther") },
    { sessions: [session("sesChild", "sesRoot"), session("sesRoot", undefined, "/outside")] },
    { sessions: [session("sesChild", "sesChild")] },
    { sessions: [session("sesChild", "../escape")] },
  ]) {
    const h = await harness(options)
    await assert.rejects(h.tools.get("shell").execute({}, contextFor("sesChild")))
    await assert.rejects(h.tools.get("github_track_pr").execute({}, contextFor("sesChild")))
    assert.deepEqual(h.effects, [])
    assert.ok(h.lookups.length <= 128)
    h.cleanup()
  }
})

test("deletion is location-scoped and waits for runtime stop", async () => {
  let release
  const h = await harness({ stopExit: new Promise(resolve => { release = resolve }) })
  await h.deleted("sesRoot", "/other")
  assert.deepEqual(h.effects, [])
  await h.deleted("sesChild")
  assert.deepEqual(h.effects.map(e => e.type), ["stop"])
  assert.deepEqual(h.effects[0].command.slice(1), ["stop", "--session", "sesChild", "--directory", directory])
  release(0)
  await tick()
  assert.equal(h.effects.at(-1).path, `${tmp}/sesChild`)
  h.cleanup()
})

test("failed runtime stop preserves temporary files", async () => {
  const h = await harness({ stopExit: Promise.resolve(1) })
  await h.deleted("sesRoot")
  assert.equal(h.effects.some(e => e.type === "rm"), false)
  assert.equal(h.errors.length, 1)
  h.cleanup()
})

test("locationless V2 deletions clean up only the exact session in this workspace", async () => {
  const h = await harness({ get: () => { throw new Error("Deleted sessions cannot be queried") } })
  await h.deleted("sesRoot/../sesOther", null)
  assert.deepEqual(h.effects, [])
  for (const id of ["sesChild", "sesRoot"]) {
    h.effects.length = 0
    await h.deleted(id, null)
    assert.deepEqual(h.effects.map(e => e.type), ["stop", "exit", "rm"])
    assert.deepEqual(h.effects[0].command.slice(1), ["stop", "--session", id, "--directory", directory])
    assert.equal(h.effects.at(-1).path, `${tmp}/${id}`)
  }
  assert.deepEqual(h.lookups, [])
  h.cleanup()
})

test("V2 file and patch tools cannot access shell-private temporary files", async () => {
  const h = await harness()
  for (const tool of ["read", "glob", "grep", "edit", "write"]) {
    await assert.rejects(h.hooks.before({ tool, input: { path: "/tmp/file" } }), /temporary files/)
    await h.hooks.before({ tool, input: { path: `${directory}/file` } })
  }
  await assert.rejects(h.hooks.before({ tool: "patch", input: { patchText: "*** Begin Patch\n*** Add File: /tmp/file\n+x\n*** End Patch" } }), /temporary files/)
  h.cleanup()
})
