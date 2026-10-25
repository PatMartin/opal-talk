// Generates src/api/operations.json from the authoritative contract (../../opal_talk.yaml).
// Run automatically by the dev/test/build/typecheck npm scripts; the file is committed
// so editors and CI see a stable artifact, and parity tests fail if it goes stale.
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse } from 'yaml'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const spec = parse(readFileSync(resolve(root, '../opal_talk.yaml'), 'utf8'))
const METHODS = new Set(['get', 'put', 'post', 'patch', 'delete'])

const out = {}
for (const [path, item] of Object.entries(spec.paths)) {
  for (const [method, op] of Object.entries(item)) {
    if (METHODS.has(method)) out[op.operationId] = { method: method.toUpperCase(), path }
  }
}
const target = resolve(root, 'src/api/operations.json')
const json = JSON.stringify(out, null, 2) + '\n'
if (!existsSync(target) || readFileSync(target, 'utf8') !== json) {
  writeFileSync(target, json)
}
console.log(`operations.json: ${Object.keys(out).length} operations`)
