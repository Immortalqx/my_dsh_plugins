#!/usr/bin/env node
/**
 * verify-contract.mjs — regression harness for the plugins in this repo,
 * checked against the *installed* `@deepseek-ai/dsh` release.
 *
 * Why this exists
 * ---------------
 * Both plugins register their model-facing tools through `defineTool()` from
 * `@deepseek-ai/dsh-tools`. That compiler is strict and versioned: it lifts
 * per-property `required: true` into a root `required[]`, demands an explicit
 * `additionalProperties` on every object node, forbids `required` at a value
 * root, and rejects any author key it does not know. A DSH upgrade can change
 * those rules (or the exec-context shape, or the render contract), and the
 * failure mode is quiet — the tool loads, then the model sees a collapsed
 * schema or the call throws at runtime. None of that is visible from reading
 * the plugin source, which is why this file exists: it re-compiles both tool
 * definitions against whatever DSH is installed and fails loudly. Run it after
 * every DSH upgrade.
 *
 * What it checks, per plugin
 * --------------------------
 *   1. the module loads from the installed location and exports
 *      `name` / `inject` / `apply`;
 *   2. `apply(ctx)` registers exactly one tool, under the expected name;
 *   3. the tool carries a non-empty description and an executable `execute`;
 *   4. the compiled `parameters` JSON schema is object-rooted and still
 *      declares the expected properties, types, required list, and
 *      per-property descriptions;
 *   5. `validateJsonSchemaValue()` rejects malformed arguments and accepts
 *      well-formed ones — i.e. the model-facing argument contract is enforced;
 *   6. a representative result still validates against `output.schema`;
 *   7. `output.render()` returns model-visible text parts;
 *   8. with `--smoke`, `execute()` runs for real and its result is validated
 *      against `output.schema` — the end-to-end path.
 *
 * It reads the INSTALLED plugin (default `$DSH_HOME/plugins`, else
 * `~/.dsh/plugins`) because the plugin's own `import '@deepseek-ai/dsh-tools'`
 * resolves through the `node_modules` junction that `install.py` creates
 * there. Verifying a repo checkout instead would fail on that import, not on
 * the contract under test.
 *
 * Usage
 * -----
 *   node tools/verify-contract.mjs                     # verify the real install
 *   node tools/verify-contract.mjs --dsh-home <dir>    # verify a --target-home install
 *   node tools/verify-contract.mjs --dsh <pkg-dir>     # explicit DSH package dir
 *   node tools/verify-contract.mjs --dump-schema       # print model-facing schemas
 *   node tools/verify-contract.mjs --smoke             # also execute both tools
 *
 * `--dsh-home` means the DSH home itself (the `.dsh` directory, exactly like
 * `$DSH_HOME`). Passing a directory that only *contains* `.dsh` is detected
 * and descended into, so install.py's `--target-home <user-home>` and this
 * flag stay interchangeable in practice.
 *
 * Exit code 0 = every check passed. Requires Node >= 18.
 */

import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

// ---- expected model-facing contracts -------------------------------------
//
// One entry per plugin. `properties` is what the model must see in the
// compiled schema; `required` is the list the compiler must lift out of the
// per-property `required: true` markers. Keep these in step with the plugin
// sources — that is the point of the harness.

const PLUGINS = [
  {
    dir: 'web-search-mmx',
    tool: 'web_search',
    properties: {
      queries: { type: 'array', items: { type: 'string' } },
    },
    required: ['queries'],
    sampleArgs: { queries: ['deepseek harness plugin contract'] },
    malformedArgs: [
      { label: 'missing queries', args: {} },
      { label: 'wrong type (string)', args: { queries: 'one query' } },
      { label: 'wrong item type', args: { queries: [42] } },
    ],
    sampleResult: {
      sources: [
        { url: 'https://example.com/a', title: 'A', snippet: 'snippet', publishedAt: '2026-01-01' },
        { url: 'https://example.com/b' },
      ],
      truncated: false,
      queriesRun: 2,
      errors: [],
    },
    smokeArgs: { queries: ['DeepSeek Harness'] },
  },
  {
    dir: 'web-fetch-local',
    tool: 'web_fetch',
    properties: {
      url: { type: 'string' },
    },
    required: ['url'],
    sampleArgs: { url: 'https://example.com/' },
    malformedArgs: [
      { label: 'missing url', args: {} },
      { label: 'wrong type (number)', args: { url: 7 } },
    ],
    sampleResult: {
      ok: true,
      title: 'Example',
      url: 'https://example.com/',
      statusCode: 200,
      bytes: 1256,
      truncated: false,
      contentType: 'text/html',
      format: 'markdown',
      htmlToMdEngine: 'turndown+gfm',
      metadata: null,
      metadataKind: 'none',
      extructAvailable: true,
      textChars: 100,
      previewChars: 100,
      bodyLines: 10,
      resourcesLines: 0,
      resourcesStartLine: null,
      metadataLines: 0,
      metadataStartLine: null,
      totalBytes: 1256,
      totalLines: 10,
      linksCount: 0,
      imagesCount: 0,
      fullPath: 'C:\\Users\\you\\.dsh\\cache\\web-fetch\\web-fetch_deadbeef.txt',
      preview: '# Example',
      error: '',
    },
    smokeArgs: { url: 'https://example.com/' },
  },
]

// ---- small reporting helpers ---------------------------------------------

const results = []
let current = ''

function section(title) {
  current = title
  console.log(`\n${title}`)
}

function ok(label) {
  results.push({ group: current, label, pass: true })
  console.log(`  ok    ${label}`)
}

function fail(label, detail) {
  results.push({ group: current, label, pass: false, detail })
  console.log(`  FAIL  ${label}${detail ? ` — ${detail}` : ''}`)
}

function check(label, condition, detail) {
  if (condition) ok(label)
  else fail(label, detail)
}

// ---- locating the installed DSH and the plugin dirs ----------------------

function parseArgs(argv) {
  const out = { dshHome: null, dsh: null, dumpSchema: false, smoke: false }
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i]
    if (arg === '--dsh-home' || arg === '--home') out.dshHome = argv[++i]
    else if (arg === '--dsh') out.dsh = argv[++i]
    else if (arg === '--dump-schema') out.dumpSchema = true
    else if (arg === '--smoke') out.smoke = true
    else if (arg === '--help' || arg === '-h') {
      console.log(readFileSync(new URL(import.meta.url), 'utf8').split('*/')[0].replace(/^#![^\n]*\n/, '').replace(/^\/\*\*?/, ''))
      process.exit(0)
    } else {
      console.error(`error: unknown argument ${arg}`)
      process.exit(2)
    }
  }
  return out
}

/**
 * The DSH home this run verifies: --dsh-home, else $DSH_HOME, else ~/.dsh.
 * A directory that merely *contains* a `.dsh` directory is descended into, so
 * `install.py --target-home <user-home>` and this flag interoperate.
 */
function resolveDshHome(args) {
  let home
  if (args.dshHome) home = resolve(args.dshHome)
  else if (process.env.DSH_HOME) home = resolve(process.env.DSH_HOME)
  else home = join(homedir(), '.dsh')

  if (!existsSync(join(home, 'plugins')) && existsSync(join(home, '.dsh', 'plugins'))) {
    console.log(`  note          ${home} holds a .dsh directory; using ${join(home, '.dsh')}`)
    home = join(home, '.dsh')
  }
  return home
}

/** Locate the installed `@deepseek-ai/dsh` package directory. */
function findDshPackage(args) {
  const candidates = []
  if (args.dsh) candidates.push(resolve(args.dsh))
  if (process.platform === 'win32') {
    candidates.push(join(homedir(), 'AppData', 'Roaming', 'npm', 'node_modules', '@deepseek-ai', 'dsh'))
    candidates.push(join('C:\\', 'Program Files', 'nodejs', 'node_modules', '@deepseek-ai', 'dsh'))
  } else {
    candidates.push('/usr/local/lib/node_modules/@deepseek-ai/dsh')
    candidates.push('/opt/homebrew/lib/node_modules/@deepseek-ai/dsh')
    candidates.push(join(homedir(), '.npm-global', 'lib', 'node_modules', '@deepseek-ai', 'dsh'))
  }
  for (const dir of candidates) {
    if (existsSync(join(dir, 'node_modules', '@deepseek-ai', 'dsh-tools', 'lib', 'index.js'))) return dir
  }
  console.error('error: could not locate the installed @deepseek-ai/dsh package.')
  console.error(`       tried: ${candidates.join(', ')}`)
  console.error('       pass --dsh <path-to>/node_modules/@deepseek-ai/dsh')
  process.exit(2)
}

// ---- main ----------------------------------------------------------------

const args = parseArgs(process.argv.slice(2))
const dshHome = resolveDshHome(args)
const dshPackage = findDshPackage(args)
const dshVersion = JSON.parse(readFileSync(join(dshPackage, 'package.json'), 'utf8')).version
const toolsEntry = join(dshPackage, 'node_modules', '@deepseek-ai', 'dsh-tools', 'lib', 'index.js')
const dshTools = await import(pathToFileURL(toolsEntry).href)

console.log('verify-contract: plugin tool contracts against the installed DSH')
console.log(`  DSH package : ${dshPackage}`)
console.log(`  DSH version : ${dshVersion}`)
console.log(`  DSH home    : ${dshHome}`)
console.log(`  dsh-tools   : ${toolsEntry}`)

// A compiled tool definition is what `ctx.tools.register()` receives; capture
// it instead of running a whole cordis app.
function captureRegistrations(pluginModule) {
  const captured = []
  const ctx = { tools: { register: (definition) => captured.push(definition) } }
  pluginModule.apply(ctx)
  return captured
}

for (const spec of PLUGINS) {
  section(`${spec.dir}  ->  ${spec.tool}`)
  const pluginDir = join(dshHome, 'plugins', spec.dir)
  const entry = join(pluginDir, 'src', 'index.mjs')

  if (!existsSync(entry)) {
    fail('plugin source present', `not found: ${entry} (run install.py first, or pass --home)`)
    continue
  }
  check('plugin source present', true)

  // 1. module shape
  let mod
  try {
    mod = await import(pathToFileURL(entry).href)
    ok('module loads (its own import of @deepseek-ai/dsh-tools resolves)')
  } catch (error) {
    fail('module loads', error.message.split('\n')[0])
    continue
  }
  check('exports apply()', typeof mod.apply === 'function')
  check('declares inject: [tools]', Array.isArray(mod.inject) && mod.inject.includes('tools'),
    `inject = ${JSON.stringify(mod.inject)}`)

  // 2. registration
  let captured
  try {
    captured = captureRegistrations(mod)
  } catch (error) {
    fail('apply(ctx) registers without throwing', error.message.split('\n')[0])
    continue
  }
  check('registers exactly one tool', captured.length === 1, `registered ${captured.length}`)
  const def = captured[0]
  if (!def) continue
  check(`tool name is "${spec.tool}"`, def.name === spec.tool, `got "${def.name}"`)
  check('description is a non-empty string', typeof def.description === 'string' && def.description.length > 0)
  check('execute() is a function', typeof def.execute === 'function')
  check('output.render() is a function (required by register() in 0.1.5)',
    typeof def.output?.render === 'function')

  // 3. compiled parameter schema
  const params = def.parameters
  check('parameters compiled to an object schema', params?.type === 'object',
    `type = ${JSON.stringify(params?.type)}`)
  for (const [key, expected] of Object.entries(spec.properties)) {
    const prop = params?.properties?.[key]
    if (!prop) {
      fail(`parameters.${key} present`, 'missing from the compiled schema')
      continue
    }
    check(`parameters.${key}.type is ${expected.type}`, prop.type === expected.type,
      `got ${JSON.stringify(prop.type)}`)
    if (expected.items) {
      check(`parameters.${key}.items.type is ${expected.items.type}`,
        prop.items?.type === expected.items.type, `got ${JSON.stringify(prop.items?.type)}`)
    }
    check(`parameters.${key} documents itself for the model`,
      typeof prop.description === 'string' && prop.description.length > 0)
  }
  for (const key of spec.required) {
    check(`parameters.required includes "${key}"`,
      Array.isArray(params?.required) && params.required.includes(key),
      `required = ${JSON.stringify(params?.required)}`)
  }

  // 4. the argument contract the model is held to
  const validViolations = dshTools.validateJsonSchemaValue(def.parameters, spec.sampleArgs, '')
  check('well-formed arguments validate', validViolations.length === 0, validViolations.join('; '))
  for (const bad of spec.malformedArgs) {
    const violations = dshTools.validateJsonSchemaValue(def.parameters, bad.args, '')
    check(`malformed arguments rejected: ${bad.label}`, violations.length > 0)
  }

  // 5. output contract
  check('output.schema is present', def.output?.schema !== undefined)
  const resultViolations = dshTools.validateJsonSchemaValue(def.output.schema, spec.sampleResult, '')
  check('representative result validates against output.schema',
    resultViolations.length === 0, resultViolations.join('; '))
  let parts
  try {
    parts = def.output.render(spec.sampleArgs, spec.sampleResult)
  } catch (error) {
    fail('output.render() runs on a representative result', error.message.split('\n')[0])
  }
  if (parts !== undefined) {
    check('output.render() returns text parts',
      Array.isArray(parts) && parts.length > 0 && parts.every((p) => p?.type === 'text' && typeof p.text === 'string'),
      JSON.stringify(parts)?.slice(0, 120))
  }

  // 6. optional live execution
  if (args.smoke) {
    const signal = new AbortController().signal
    try {
      const value = await def.execute(spec.smokeArgs, { signal, callId: 'verify-contract-smoke' })
      ok('smoke: execute() resolved')
      const violations = dshTools.validateJsonSchemaValue(def.output.schema, value, '')
      check('smoke: real result validates against output.schema',
        violations.length === 0, violations.join('; '))
      const rendered = def.output.render(spec.smokeArgs, value)
      check('smoke: real result renders',
        Array.isArray(rendered) && rendered.length > 0 &&
        rendered.every((p) => p?.type === 'text' && typeof p.text === 'string'))
    } catch (error) {
      fail('smoke: execute() resolved', error.message.split('\n')[0])
    }
  }

  if (args.dumpSchema) {
    console.log(`  parameters  : ${JSON.stringify(def.parameters)}`)
    console.log(`  output      : ${JSON.stringify(def.output.schema)}`)
  }
}

// ---- summary -------------------------------------------------------------

const failed = results.filter((r) => !r.pass)
console.log(`\n${results.length - failed.length}/${results.length} checks passed`)
if (failed.length > 0) {
  console.log('\nfailed:')
  for (const f of failed) console.log(`  - ${f.group}: ${f.label}${f.detail ? ` — ${f.detail}` : ''}`)
  process.exit(1)
}
console.log(`\ncontract verified against @deepseek-ai/dsh ${dshVersion}`)
if (!args.smoke) console.log('(re-run with --smoke to execute both tools end to end)')
