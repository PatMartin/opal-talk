import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { parse } from 'yaml'
import operations from '../api/operations.json'

const root = resolve(__dirname, '../..')
const spec = parse(readFileSync(resolve(root, '../opal_talk.yaml'), 'utf8'))

function contractOperations(): Record<string, { method: string; path: string }> {
  const out: Record<string, { method: string; path: string }> = {}
  for (const [path, item] of Object.entries<Record<string, { operationId: string }>>(spec.paths)) {
    for (const [method, op] of Object.entries(item)) {
      if (['get', 'put', 'post', 'patch', 'delete'].includes(method)) out[op.operationId] = { method: method.toUpperCase(), path }
    }
  }
  return out
}

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) return ['api', 'test'].includes(name) ? [] : sources(p)
    return /\.tsx?$/.test(name) ? [readFileSync(p, 'utf8')] : []
  })
}

describe('SPA / REST contract parity', () => {
  it('operation map matches opal_talk.yaml exactly', () => {
    expect(operations).toEqual(contractOperations())
  })

  it('every REST operation is used by the UI', () => {
    const code = sources(resolve(root, 'src')).join('\n')
    const unused = Object.keys(operations).filter((op) => !code.includes(`'${op}'`))
    expect(unused).toEqual([])
  })
})
