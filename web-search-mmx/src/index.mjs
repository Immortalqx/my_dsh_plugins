// web-search-mmx — `web_search` tool backed by local mmx-cli (cross-platform).
//
// Replaces the shipped `web_search` (which bills DEEPSEEK_API_KEY) with an
// mmx-cli implementation that bills MiniMax OAuth.
//
// Tool name (model-facing)  : `web_search` (drop-in)
// Plugin row id             : `web-search-mmx`
// Args                      : `{ queries: string[] }`  (1..4, exact duplicates collapse)
// Output                    : `{ sources: [{url, title?, snippet?, publishedAt?}], truncated, errors }`
//
// Interface note (DSH >= 0.1.5): the shipped `web_search` takes a `queries`
// ARRAY, runs them concurrently, merges the sources round-robin, and caps the
// combined list at `searchMaxResults` (8). This plugin mirrors that argument
// surface so it stays a true drop-in. Two deliberate differences:
//
//   - The shipped tool also returns an optional provider `answer`; mmx-cli's
//     `search query` returns only organic results (title/link/snippet/date),
//     so there is no `answer` field here.
//   - The shipped tool aborts the whole call when any one query fails and
//     discards the successful results. mmx-cli is a local subprocess with its
//     own rate limits, so a single flaky query should not void a four-query
//     search: failures are reported per query in `errors`, and only an
//     all-queries-failed call throws.
//
// Schema pipeline note:
//   The tool definition is wrapped in `defineTool()` from
//   `@deepseek-ai/dsh-tools` (the installed compiler; run
//   `node tools/verify-contract.mjs` to re-verify it against your DSH).
//   `defineTool()` runs `parameterSchemaSpecToJsonSchema()`, which lifts
//   per-property `required: true` into a top-level `required[]` and wraps the
//   spec in `{type:"object", properties, required}`. Without this wrapper the
//   model-facing `parameters` schema collapses to `{properties:{}, required:[]}`
//   because `ctx.tools.register()` does NOT compile raw parameter maps.
//   Constraints the compiler enforces on `output.schema` (a *spec* DSL, not raw
//   JSON Schema): every object node needs an explicit boolean
//   `additionalProperties`; a value root may not carry `required`; `oneOf`
//   needs at least two branches; only the author keys
//   description/title/default/examples/type/enum/const/items/oneOf/properties/
//   additionalProperties are accepted.
//
//   `@deepseek-ai/dsh-tools` is resolved via the node_modules junction that
//   `install.py` creates at `~/.dsh/plugins/web-search-mmx/node_modules`
//   pointing into DSH's bundled `<DSH root>/node_modules/@deepseek-ai/dsh/
//   node_modules/`.
//
// Cross-platform mmx invocation:
//   - Windows: `cmd.exe /c mmx <args>`  — Node 18+ does not consult PATHEXT
//     and CVE-2024-27980 blocks direct spawn of .cmd/.bat. args are passed
//     as separate argv entries (no shell-string concatenation, no injection).
//   - macOS/Linux: bare `mmx` works; mmx-cli is typically a shell wrapper
//     symlinked into /usr/local/bin or ~/.local/bin.
// Override with `DSH_WEB_SEARCH_MMX_BIN=...` if `mmx` is not on PATH.

import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { defineTool } from '@deepseek-ai/dsh-tools'

const exec = promisify(execFile)

const DEFAULT_TIMEOUT_MS = 30_000
const MAX_BUFFER_BYTES = 4 * 1024 * 1024
const MAX_QUERIES = 4        // mirror of the shipped `searchMaxQueries` default
const MAX_QUERY_CHARS = 500
const RESULT_LIMIT = 8       // mirror of the shipped `searchMaxResults` default

export const name = 'web-search-mmx'
export const inject = ['tools']

/**
 * Run one `mmx search query`. Returns the raw organic result rows:
 *   [{ title?, link?, snippet?, date? }, ...]
 */
async function runMmxQuery(query, timeoutMs, signal) {
  const mmxArgs = ['search', 'query', '--q', query, '--output', 'json',
                    '--quiet', '--non-interactive']
  const isWin = process.platform === 'win32'
  // Per-platform mmx invocation. On Windows always go through `cmd.exe /c`:
  // npm installs mmx as a `.cmd` shim, and Node >= 18 refuses to spawn a
  // `.cmd`/`.bat` directly (CVE-2024-27980) with `EINVAL` — which is also why
  // `DSH_WEB_SEARCH_MMX_BIN` may not point at a shim and be spawned raw.
  // No shell-string concatenation: the binary and every argument travel as
  // separate argv entries, so a query can never become shell syntax.
  const bin = process.env.DSH_WEB_SEARCH_MMX_BIN || 'mmx'
  const cmd = isWin ? 'cmd.exe' : bin
  const args = isWin ? ['/c', bin, ...mmxArgs] : mmxArgs
  const { stdout } = await exec(cmd, args, {
    timeout: timeoutMs,
    maxBuffer: MAX_BUFFER_BYTES,
    signal,
    windowsHide: true,
  })

  let parsed
  try {
    parsed = JSON.parse(stdout)
  } catch (e) {
    throw new Error(`mmx output was not valid JSON: ${e.message}\n--- first 500 chars ---\n${stdout.slice(0, 500)}`)
  }
  return Array.isArray(parsed.organic) ? parsed.organic : []
}

/** Normalise one mmx row into the model-facing source shape (dropping empties). */
function toSource(row) {
  const url = row?.link ?? ''
  if (!url) return null
  // Strip undefined values — DSH lossless-JSON validation rejects them.
  const source = { url }
  if (row.title) source.title = row.title
  if (row.snippet) source.snippet = row.snippet
  if (row.date) source.publishedAt = row.date
  return source
}

/**
 * Merge per-query result lists round-robin (one from each query in turn, then
 * the next round), dropping duplicate URLs and capping the total. Round-robin
 * keeps a multi-query call diverse instead of exhausting the first query's
 * results first, which is the order the shipped tool uses.
 */
function mergeRoundRobin(perQuery, limit) {
  const sources = []
  const seen = new Set()
  let dropped = 0
  const depth = Math.max(0, ...perQuery.map((rows) => rows.length))
  for (let i = 0; i < depth; i++) {
    for (const rows of perQuery) {
      const source = toSource(rows[i])
      if (!source) continue
      if (seen.has(source.url)) continue
      seen.add(source.url)
      if (sources.length >= limit) { dropped++; continue }
      sources.push(source)
    }
  }
  return { sources, truncated: dropped > 0 }
}

/** Human-readable message for one failed query, including mmx's stderr. */
function describeFailure(err, timeoutMs, binHint) {
  if (err && err.name === 'AbortError') return 'cancelled'
  if (err && (err.code === 'ETIMEDOUT' || err.killed)) return `timed out after ${timeoutMs}ms`
  if (err && err.code === 'ENOENT') {
    return `${binHint} not found on PATH — install mmx-cli and authenticate with \`mmx auth login\``
  }
  // mmx's own stderr is the useful part: it names the real cause (auth,
  // quota, network). Node's execFile message instead leads with
  // "Command failed: <entire command line>", which is pure noise in the
  // model's context, so prefer stderr and drop that prefix otherwise.
  const stderr = err && err.stderr ? String(err.stderr).trim() : ''
  if (stderr) return stderr.replace(/\s+/g, ' ').slice(0, 400)
  const message = err && err.message ? String(err.message) : String(err)
  const lines = message.split('\n').map((l) => l.trim()).filter(Boolean)
  const useful = lines.filter((l) => !l.startsWith('Command failed:'))
  return (useful.length > 0 ? useful.join(' ') : lines[0] || message).slice(0, 400)
}

export function apply(ctx) {
  ctx.tools.register(defineTool({
    name: 'web_search',

    description:
      'Search the web via MiniMax OAuth (mmx-cli). Pass 1 to 4 `queries`: they run ' +
      'concurrently, exact duplicates collapse, and their sources merge round-robin ' +
      'into at most 8 results with url, title, snippet, and publishedAt. **A snippet ' +
      'is a brief excerpt, not full content** — `web_fetch` reads the complete page ' +
      'from a URL. Query-level failures come back in `errors` while the successful ' +
      "queries' sources are still returned.",

    parameters: {
      queries: {
        type: 'array',
        required: true,
        items: { type: 'string' },
        description:
          'One to four non-empty search queries (each 1-500 chars). Exact duplicates ' +
          'run once. Use several short, differently-worded queries instead of one long one.',
      },
    },

    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          sources: {
            type: 'array',
            items: {
              type: 'object',
              additionalProperties: false,
              properties: {
                url: { type: 'string' },
                title: { type: 'string' },
                snippet: { type: 'string' },
                publishedAt: { type: 'string' },
              },
            },
          },
          truncated: { type: 'boolean' },
          queriesRun: { type: 'number' },
          errors: {
            type: 'array',
            items: { type: 'string' },
          },
        },
      },
      render(args, value) {
        const lines = []
        const run = typeof value.queriesRun === 'number' ? value.queriesRun : 0
        const failed = Array.isArray(value.errors) ? value.errors.length : 0
        const label = run > 1 ? `${run} queries` : '1 query'
        const failedTag = failed > 0 ? `, ${failed} failed` : ''
        lines.push(`web_search: ${value.sources.length} result(s) from ${label}${failedTag} via mmx-cli (MiniMax OAuth)`)
        for (const s of value.sources) {
          const title = s.title || s.url
          lines.push(`- [${title}](${s.url})`)
          if (s.snippet) lines.push(`  ${s.snippet.replace(/\s+/g, ' ').slice(0, 280)}`)
        }
        if (value.sources.length === 0) lines.push('No results.')
        if (value.truncated) {
          lines.push(`(Showing the first ${value.sources.length} sources. Refine the queries for more.)`)
        }
        if (Array.isArray(value.errors) && value.errors.length > 0) {
          lines.push('Failed queries:')
          for (const e of value.errors) lines.push(`- ${String(e).replace(/\s+/g, ' ').slice(0, 300)}`)
        }
        lines.push('Cite the relevant URLs above as markdown links in your answer.')
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },

    async execute(args, execCtx) {
      const raw = Array.isArray(args?.queries) ? args.queries : []
      // Exact-string dedupe first: the schema already guarantees an array of
      // strings, but not how many of them are the same query.
      const queries = []
      for (const entry of raw) {
        const q = String(entry ?? '').trim()
        if (q === '') throw new Error('queries must not contain an empty string')
        if (q.length > MAX_QUERY_CHARS) throw new Error(`each query must be <= ${MAX_QUERY_CHARS} chars`)
        if (!queries.includes(q)) queries.push(q)
      }
      if (queries.length === 0) throw new Error('queries must contain at least one query')
      if (queries.length > MAX_QUERIES) {
        throw new Error(`queries must contain at most ${MAX_QUERIES} queries, got ${queries.length}`)
      }

      // Fan out concurrently; `allSettled` keeps one failed query from
      // discarding the others (see the header note on that deviation).
      const settled = await Promise.allSettled(
        queries.map((q) => runMmxQuery(q, DEFAULT_TIMEOUT_MS, execCtx.signal)),
      )

      const perQuery = []
      const errors = []
      for (let i = 0; i < settled.length; i++) {
        const outcome = settled[i]
        if (outcome.status === 'fulfilled') {
          perQuery.push(outcome.value)
        } else {
          perQuery.push([])
          errors.push(`${queries[i]}: ${describeFailure(outcome.reason, DEFAULT_TIMEOUT_MS, 'mmx')}`)
        }
      }

      // Every query failed: surface a real tool error rather than a silent
      // empty success. With a single query this is the pre-0.1.5 behaviour.
      if (errors.length === queries.length) {
        if (execCtx.signal?.aborted) throw new Error('web_search: cancelled')
        throw new Error(`web_search: ${errors[0]}`)
      }

      const { sources, truncated } = mergeRoundRobin(perQuery, RESULT_LIMIT)
      return { sources, truncated, queriesRun: queries.length, errors }
    },
  }))
}
