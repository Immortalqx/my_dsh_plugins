// web-fetch-local — `web_fetch` tool backed by local Python urllib + SSRF blocklist.
//
// Tool name (model-facing) : `web_fetch` (drop-in)
// Plugin row id            : `web-fetch-local`
// Args                     : `{ url, max_bytes? }`
// Output                   : `{ title, url, statusCode, bytes, totalBytes, truncated,
//                               textChars, previewChars, bodyLines, resourcesLines,
//                               resourcesStartLine, totalLines, linksCount,
//                               imagesCount, fullPath, preview, error }`
//   - `title` is the page <title> when available, else ""
//   - `error` is non-empty only on ok=false responses
//
// The tool returns a short preview inline (first ~2000 chars of the
// extracted text). The full extracted text is always written to a cache
// file under ~/.dsh/cache/web-fetch/ — the model can re-read it with the
// built-in `str_replace_editor` `view` command when it needs the whole
// page (long articles, references, etc.).
//
// Why Python (not Node fetch): separate process boundary makes sandbox
// leakage easier to reason about — the Node side never opens a raw socket
// itself.
//
// Cross-platform python invocation:
//   - Windows: `python` (.py is associated with the Microsoft Store or
//     python.org installer).
//   - macOS/Linux: `python3` (modern macOS has no `python` symlink).
// Override with `DSH_WEB_FETCH_PYTHON=...` if your installation differs.
//
// Host policy: any public host is allowed; only loopback / private suffixes
// are blocked (SSRF defence).

import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const exec = promisify(execFile)
const HERE = dirname(fileURLToPath(import.meta.url))

const DEFAULT_MAX_BYTES = 500_000
const HARD_MAX_BYTES = 2_000_000
const HARD_MIN_BYTES = 5_000
const DEFAULT_TIMEOUT_MS = 20_000  // subprocess timeout; fetch.py default is 15s

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
      'Fetch the text content of an HTTP(S) URL. Returns a short inline preview ' +
      '(first ~2000 chars) and the path to a cache file with the full extracted ' +
      'text. **The inline preview is truncated** — read the cache file with ' +
      '`str_replace_editor` to see the complete page.',

    parameters: {
      url: {
        type: 'string',
        required: true,
        description: 'Absolute http(s) URL to fetch.',
      },
      max_bytes: {
        type: 'number',
        description:
          'Hard byte cap on the raw HTTP body (5000-2000000). Default 500000 ' +
          '(~500KB). For short snippets pass ~50000; for huge pages pass 2000000.',
      },
    },

    output: {
      schema: {
        type: 'object',
        properties: {
          title: { type: 'string' },
          url: { type: 'string' },
          statusCode: { type: 'number' },
          bytes: { type: 'number' },
          totalBytes: { type: 'number' },
          truncated: { type: 'boolean' },
          textChars: { type: 'number' },
          previewChars: { type: 'number' },
          bodyLines: { type: 'number' },
          resourcesLines: { type: 'number' },
          resourcesStartLine: { type: ['number', 'null'] },
          totalLines: { type: 'number' },
          linksCount: { type: 'number' },
          imagesCount: { type: 'number' },
          fullPath: { type: 'string' },
          preview: { type: 'string' },
          error: { type: 'string' },
        },
        required: ['url', 'statusCode', 'textChars'],
      },
      render(_args, value) {
        if (!value.statusCode || value.statusCode >= 400) {
          return [{ type: 'text', text: `${value.url} (HTTP ${value.statusCode || 'ERR'})\nError: ${value.error || ''}` }]
        }
        const titleLine = value.title ? `Title: ${value.title}\n` : ''
        const head = `${value.url} (HTTP ${value.statusCode}, ${value.bytes} bytes${value.truncated ? ', truncated' : ', complete'})`
        const hasResources = value.resourcesStartLine !== null && value.resourcesStartLine !== undefined
        const layoutLines = []
        layoutLines.push(`Body: ${value.textChars} chars, lines 1-${value.bodyLines}`)
        if (hasResources) {
          layoutLines.push(`Resources: ${value.linksCount} links + ${value.imagesCount} images, lines ${value.resourcesStartLine}-${value.totalLines}`)
        }
        const cacheHint = value.fullPath
          ? `\n\n${layoutLines.join('\n')}\n\n` +
            (value.bodyLines > 0
              ? `Read body:      str_replace_editor(command="view", path="${value.fullPath}", view_range=[1, ${value.bodyLines}])\n`
              : '') +
            (hasResources
              ? `Read resources: str_replace_editor(command="view", path="${value.fullPath}", view_range=[${value.resourcesStartLine}, -1])\n`
              : '') +
            `Read whole:     str_replace_editor(command="view", path="${value.fullPath}", view_range=[1, -1])`
          : ''
        return [{ type: 'text', text: `${titleLine}${head}\n\n${value.preview}${cacheHint}` }]
      },
    },

    async execute(args, execCtx) {
      const url = String(args.url ?? '').trim()
      if (!url) throw new Error('url must be a non-empty string')
      if (!/^https?:\/\//i.test(url)) throw new Error('url must start with http:// or https://')

      let maxBytes = Number.isFinite(args.max_bytes) ? Math.floor(args.max_bytes) : DEFAULT_MAX_BYTES
      if (maxBytes < HARD_MIN_BYTES) maxBytes = HARD_MIN_BYTES
      if (maxBytes > HARD_MAX_BYTES) maxBytes = HARD_MAX_BYTES

      const scriptPath = join(HERE, 'fetch.py')
      const cmdArgs = [
        scriptPath,
        '--url', url,
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
          title: parsed.title || '',
          url: parsed.url || url,
          statusCode: parsed.status ?? 0,
          bytes: parsed.bytes ?? 0,
          totalBytes: parsed.total_bytes ?? 0,
          truncated: !!parsed.truncated,
          textChars: parsed.text_chars ?? (parsed.preview ? parsed.preview.length : 0),
          previewChars: parsed.preview_chars ?? (parsed.preview ? parsed.preview.length : 0),
          bodyLines: parsed.body_lines ?? 0,
          resourcesLines: parsed.resources_lines ?? 0,
          resourcesStartLine: parsed.resources_start_line ?? null,
          totalLines: parsed.total_lines ?? 0,
          linksCount: parsed.links_count ?? 0,
          imagesCount: parsed.images_count ?? 0,
          fullPath: parsed.full_path || '',
          preview: parsed.preview || '',
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