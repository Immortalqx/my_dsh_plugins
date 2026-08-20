// web-search-mmx — `web_search` tool backed by local mmx-cli (cross-platform).
//
// Replaces shipped `web_search` (which bills DEEPSEEK_API_KEY) with an
// mmx-cli implementation that bills MiniMax OAuth.
//
// Tool name (model-facing)  : `web_search` (drop-in)
// Plugin row id             : `web-search-mmx`
// Args                      : `{ query: string }`
// Output                    : `{ sources: [{url, title?, snippet?, publishedAt?}], truncated }`
//
// Schema pipeline note:
//   The tool definition is wrapped in `defineTool()` from
//   `@deepseek-ai/dsh-tools` (see web-fetch-local/docs/RESEARCH-NOTES.md,
//   "DSH 模型可见 schema 的端到端管线"). Without this wrapper the model-facing
//   `parameters` schema collapses to `{properties:{}, required:[]}` because
//   `ctx.tools.register()` does NOT compile raw parameter maps — only the
//   `output.schema`. `defineTool()` runs `parameterSchemaSpecToJsonSchema()`
//   which lifts per-property `required: true` into a top-level `required[]`
//   and wraps the spec in `{type:"object", properties, required}`. The
//   `output.schema` here uses `additionalProperties: false` and avoids
//   top-level `required: [...]` because the dsh-tools compiler rejects them.
//
//   `@deepseek-ai/dsh-tools` is resolved via the node_modules junction
//   that `install.py` creates at `~/.dsh/plugins/web-search-mmx/node_modules`
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
const RESULT_LIMIT = 8  // match original `searchMaxResults: 8`

export const name = 'web-search-mmx'
export const inject = ['tools']

/**
 * Wrap `mmx search query`. Returns a normalised WebSearchResult-shaped object:
 *   { sources: [{url, title?, snippet?, publishedAt?}], truncated: boolean }
 */
async function runMmxSearch(query, opts, signal) {
  const mmxArgs = ['search', 'query', '--q', query, '--output', 'json',
                    '--quiet', '--non-interactive']
  const isWin = process.platform === 'win32'
  // Per-platform mmx invocation. cmd.exe /c mmx on Windows because of
  // PATHEXT/CVE-2024-27980 — see file header for the why.
  const cmd = process.env.DSH_WEB_SEARCH_MMX_BIN
    || (isWin ? 'cmd.exe' : 'mmx')
  const args = isWin && cmd === 'cmd.exe'
    ? ['/c', process.env.DSH_WEB_SEARCH_MMX_BIN || 'mmx', ...mmxArgs]
    : mmxArgs
  const { stdout } = await exec(cmd, args, {
    timeout: opts.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    maxBuffer: opts.maxBuffer ?? MAX_BUFFER_BYTES,
    signal,
    windowsHide: true,
  })

  let parsed
  try {
    parsed = JSON.parse(stdout)
  } catch (e) {
    throw new Error(`mmx output was not valid JSON: ${e.message}\n--- first 500 chars ---\n${stdout.slice(0, 500)}`)
  }

  const organic = Array.isArray(parsed.organic) ? parsed.organic : []
  const truncated = organic.length > RESULT_LIMIT
  const sliced = organic.slice(0, RESULT_LIMIT)
  // Strip undefined values — DSH lossless-JSON validation rejects them.
  // Only emit a field when mmx actually supplied a value.
  const sources = sliced
    .map((r) => {
      const src = { url: r.link ?? '' }
      if (r.title) src.title = r.title
      if (r.snippet) src.snippet = r.snippet
      if (r.date) src.publishedAt = r.date
      return src
    })
    .filter((s) => s.url)  // drop rows without URL

  return { sources, truncated }
}

export function apply(ctx) {
  ctx.tools.register(defineTool({
    name: 'web_search',

    description:
      'Search the web via MiniMax OAuth (mmx-cli). Returns up to 8 sources with ' +
      'url, title, snippet, and publishedAt. **The snippet is a brief excerpt, ' +
      'not full content** — `web_fetch` reads the complete page from a URL.',

    parameters: {
      query: {
        type: 'string',
        required: true,
        description: 'Search query (1-500 chars).',
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
        },
      },
      render(_args, value) {
        const lines = []
        lines.push(`web_search: ${value.sources.length} result(s) via mmx-cli (MiniMax OAuth)`)
        for (const s of value.sources) {
          const title = s.title || s.url
          lines.push(`- [${title}](${s.url})`)
          if (s.snippet) lines.push(`  ${s.snippet.replace(/\s+/g, ' ').slice(0, 280)}`)
        }
        if (value.sources.length === 0) lines.push('No results.')
        if (value.truncated) {
          lines.push(`(Showing the first ${value.sources.length} sources. Refine the query for more.)`)
        }
        lines.push('Cite the relevant URLs above as markdown links in your answer.')
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },

    async execute(args, execCtx) {
      const q = String(args.query ?? '').trim()
      if (!q) throw new Error('query must be a non-empty string')
      if (q.length > 500) throw new Error('query must be <= 500 chars')

      try {
        return await runMmxSearch(q, { timeoutMs: DEFAULT_TIMEOUT_MS }, execCtx.signal)
      } catch (err) {
        if (err && err.name === 'AbortError') {
          throw new Error('web_search: cancelled')
        }
        if (err && (err.code === 'ETIMEDOUT' || err.killed)) {
          throw new Error(`web_search: timed out after ${DEFAULT_TIMEOUT_MS}ms`)
        }
        if (err && err.code === 'ENOENT') {
          throw new Error('web_search: `mmx` not found on PATH — install mmx-cli and authenticate with `mmx auth login`')
        }
        const stderr = err && err.stderr ? `\n--- mmx stderr ---\n${String(err.stderr).slice(0, 800)}` : ''
        throw new Error(`web_search: ${err && err.message ? err.message : String(err)}${stderr}`)
      }
    },
  }))
}