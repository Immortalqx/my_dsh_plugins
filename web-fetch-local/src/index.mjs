// web-fetch-local — `web_fetch` tool backed by local Python urllib + SSRF blocklist.
//
// Tool name (model-facing) : `web_fetch` (drop-in)
// Plugin row id            : `web-fetch-local`
// Args                     : `{ url, offset_bytes?, max_bytes? }`
// Output                   : `{ url, statusCode, body, truncated, offsetBytes, totalBytes, bytes }`
//
// Why Python (not Node fetch):
//   1. Defence-in-depth — separate process boundary makes sandbox leakage
//      easier to reason about; the Node side never opens a raw socket itself.
//   2. User preference (Python SDK is on the table for future work).
//
// Cross-platform python invocation:
//   - Windows: `python` (the convention from dsh-tool-web; .py is
//     associated with the Microsoft Store or python.org installer).
//   - macOS/Linux: `python3` (modern macOS has no `python` symlink).
// Override with `DSH_WEB_FETCH_PYTHON=...` if your installation differs.
//
// Host policy: any public host is allowed; only loopback / private suffixes
// are blocked (SSRF defence). The `web_search` we replace (mmx-cli, MiniMax
// OAuth) already filters search results, so over-blocking on fetch would
// just lock the agent out of legitimate pages.

import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const exec = promisify(execFile)
const HERE = dirname(fileURLToPath(import.meta.url))

// Bumped from shipped dsh-tool-web's 200_000. Tech blogs regularly run 300-800KB.
const DEFAULT_MAX_BYTES = 500_000
const HARD_MAX_BYTES = 2_000_000
const HARD_MIN_BYTES = 5_000
const DEFAULT_TIMEOUT_MS = 20_000  // subprocess timeout; fetch.py default is 15s

// Cap on the complete rendered string. Matches shipped `web_fetch` default.
// Render truncates to this length with a footer telling the agent the body
// was cut, so it knows to call again with offset_bytes to fetch the rest.
const MAX_OUTPUT_CHARS = 200_000
const TRUNCATION_FOOTER =
  '\n\n[output truncated to fit model context; pass `offset_bytes=<prev bytes>` to web_fetch to read more]'

// Pick the Python interpreter. Override with DSH_WEB_FETCH_PYTHON if your
// install uses a non-default name (e.g., `py` on Windows, `python3.12`, or a venv path).
const PYTHON_BIN = process.env.DSH_WEB_FETCH_PYTHON
  || (process.platform === 'win32' ? 'python' : 'python3')

export const name = 'web-fetch-local'
export const inject = ['tools']

export function apply(ctx) {
  ctx.tools.register({
    name: 'web_fetch',

    description:
      'Fetch the text content of an HTTP(S) URL. To paginate long pages: pass ' +
      '`offset_bytes=<prev response bytes>` on every subsequent call. The response ' +
      'is `truncated=true` while more content is available, `truncated=false` when ' +
      'complete (you can stop). If you see the truncation footer in the output, ' +
      'call again with `offset_bytes` to read the rest. Use AFTER web_search to read ' +
      'full articles — do not fetch every search result, only the 1-3 you will cite.',

    parameters: {
      url: {
        type: 'string',
        required: true,
        description: 'Absolute http(s) URL to fetch.',
      },
      offset_bytes: {
        type: 'number',
        description:
          'Byte offset to start reading from. First call: 0. Subsequent calls: ' +
          'pass the previous response.bytes value.',
      },
      max_bytes: {
        type: 'number',
        description:
          'Hard byte cap for this call (5000-2000000). Default 500000 (~500KB). ' +
          'For short snippets pass ~50000; for huge pages pass 2000000.',
      },
    },

    output: {
      schema: {
        type: 'object',
        properties: {
          url: { type: 'string' },
          statusCode: { type: 'number' },
          body: { type: 'string' },
          truncated: { type: 'boolean' },
          offsetBytes: { type: 'number' },
          totalBytes: { type: 'number' },
          bytes: { type: 'number' },
        },
        required: ['url', 'statusCode', 'body', 'truncated'],
      },
      render(_args, value) {
        if (value.statusCode >= 400 || !value.body) {
          return [{ type: 'text', text: `${value.url} (HTTP ${value.statusCode})\nError: ${value.error || ''}` }]
        }
        const head = `${value.url} (HTTP ${value.statusCode}, ${value.bytes} bytes${value.truncated ? ', truncated' : ', complete'})`
        const tail = value.truncated
          ? `\n\nNEXT CALL: web_fetch(url="${value.url}", offset_bytes=${value.bytes})`
          : ''
        let body = value.body
        let suffix = ''
        if (body.length > MAX_OUTPUT_CHARS) {
          body = body.slice(0, MAX_OUTPUT_CHARS)
          suffix = TRUNCATION_FOOTER
        }
        return [{ type: 'text', text: `${head}${tail}\n\n${body}${suffix}` }]
      },
    },

    async execute(args, execCtx) {
      const url = String(args.url ?? '').trim()
      if (!url) throw new Error('url must be a non-empty string')
      if (!/^https?:\/\//i.test(url)) throw new Error('url must start with http:// or https://')

      const offsetBytes = Number.isFinite(args.offset_bytes) ? Math.max(0, Math.floor(args.offset_bytes)) : 0
      let maxBytes = Number.isFinite(args.max_bytes) ? Math.floor(args.max_bytes) : DEFAULT_MAX_BYTES
      if (maxBytes < HARD_MIN_BYTES) maxBytes = HARD_MIN_BYTES
      if (maxBytes > HARD_MAX_BYTES) maxBytes = HARD_MAX_BYTES

      const scriptPath = join(HERE, 'fetch.py')
      const cmdArgs = [
        scriptPath,
        '--url', url,
        '--offset-bytes', String(offsetBytes),
        '--max-bytes', String(maxBytes),
        '--timeout', '15',
      ]

      try {
        const { stdout } = await exec(PYTHON_BIN, cmdArgs, {
          timeout: DEFAULT_TIMEOUT_MS,
          maxBuffer: 8 * 1024 * 1024,
          signal: execCtx.signal,
          windowsHide: true,
        })
        let parsed
        try {
          parsed = JSON.parse(stdout)
        } catch (e) {
          throw new Error(`web_fetch: fetch.py returned non-JSON: ${e.message}\nstdout=${stdout.slice(0, 500)}`)
        }
        if (!parsed.ok) {
          throw new Error(parsed.error || `web_fetch: ${url} failed`)
        }
        return {
          url: parsed.url || url,
          statusCode: parsed.status ?? 0,
          body: parsed.text || '',
          truncated: !!parsed.truncated,
          offsetBytes: parsed.offset_bytes ?? offsetBytes,
          totalBytes: parsed.total_bytes ?? 0,
          bytes: parsed.bytes ?? 0,
        }
      } catch (err) {
        if (err && err.name === 'AbortError') throw new Error('web_fetch: cancelled')
        if (err && (err.code === 'ETIMEDOUT' || err.killed)) {
          throw new Error(`web_fetch: timed out after ${DEFAULT_TIMEOUT_MS}ms`)
        }
        if (err && err.code === 'ENOENT') {
          throw new Error(`web_fetch: \`${PYTHON_BIN}\` not found on PATH — set DSH_WEB_FETCH_PYTHON=... if your install uses a different name`)
        }
        if (err && err.message && !err.message.startsWith('web_fetch:')) {
          throw new Error(`web_fetch: ${err.message}`)
        }
        throw err
      }
    },
  })
}