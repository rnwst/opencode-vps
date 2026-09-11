import assert from 'node:assert/strict'
import { access, chmod, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { constants } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { test } from 'node:test'

assert.ok(process.env.SRT_PACKAGE, 'Set SRT_PACKAGE to the built SRT package root')
const root = resolve(process.env.SRT_PACKAGE)
const { getApplySeccompBinaryPath } = await import(pathToFileURL(
  join(root, 'dist/sandbox/generate-seccomp-filter.js'),
))
const { checkLinuxDependencies, wrapCommandWithSandboxLinux } = await import(pathToFileURL(
  join(root, 'dist/sandbox/linux-sandbox-utils.js'),
))

test('native helper is bundled and explicit invalid overrides fail closed', async t => {
  const helper = join(root, 'vendor/seccomp', process.arch, 'apply-seccomp')
  await access(helper, constants.X_OK)
  assert.equal((await readFile(helper)).subarray(0, 4).toString(), '\x7fELF')
  assert.equal(getApplySeccompBinaryPath(), helper)
  assert.equal(getApplySeccompBinaryPath(helper), helper)

  const temporary = await mkdtemp(join(tmpdir(), 'srt-helper-test-'))
  t.after(() => rm(temporary, { recursive: true, force: true }))
  const nonExecutable = join(temporary, 'non-executable')
  await writeFile(nonExecutable, 'not a helper')
  await chmod(nonExecutable, 0o600)
  for (const applyPath of [join(temporary, 'missing'), nonExecutable, temporary]) {
    assert.equal(getApplySeccompBinaryPath(applyPath), null)
    const dependencies = checkLinuxDependencies({
      seccompConfig: { applyPath }, bwrapPath: process.execPath, socatPath: process.execPath,
    })
    assert.match(dependencies.errors.join('\n'), /Required seccomp helper/)
    await assert.rejects(wrapCommandWithSandboxLinux({
      command: 'must-not-run', needsNetworkRestriction: true,
      seccompConfig: { applyPath },
    }), /Required seccomp helper/)
  }
})
