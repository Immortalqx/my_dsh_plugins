// web-fetch-local v2 — `web_fetch` tool backed by local Python urllib +
// Node-side Turndown (HTML→Markdown, reusing DSH's installed turndown +
// @joplin/turndown-plugin-gfm via install.py junction bridge).
//
// Tool name (model-facing) : `web_fetch` (drop-in)
// Plugin row id            : `web-fetch-local`
// Args                     : `{ url }`
// Output                   : see execute() return shape below.
//
// Architecture:
//   [Node] index.mjs ─→ spawns Python subprocess ─→ [Python] fetch.py
//     1. Python: HTTP + SSRF + Content-Type gate + charset decode +
//        gzip/deflate/br transport decoding + extruct (JSON-LD/OG/etc.)
//     2. Python returns: { ok, statusCode, body: <raw HTML>, metadata: {...},
//        title, body_kind, extruct_available, full_path (raw body cache), ... }
//     3. Node: Turndown converts body to Markdown, rewrites the cache file
//        to <Markdown body> + <Resources section> + <Metadata section>, and
//        returns the model-facing tool result.
//
// Why Python for HTTP and Node for Markdown?
//   - Python keeps the SSRF / network sandbox seam small and inspectable.
//   - Node + Turndown + GFM (already in DSH's deps) handles Markdown well.
//   - One subprocess boundary; no IPC round-trip during conversion.
//
// Why a junction bridge?
//   DSH installs `turndown`, `@joplin/turndown-plugin-gfm`, and
//   `@deepseek-ai/dsh-tools` under
//   `<DSH root>/node_modules/@deepseek-ai/dsh/node_modules/`. Node module
//   resolution walks up from the plugin's `index.mjs` looking for a
//   `node_modules/` directory — but it cannot see into nested
//   `@deepseek-ai/dsh/node_modules/` from our plugin path. `install.py`
//   creates a directory junction so Node finds them. If the junction is
//   missing (e.g., user skipped install.py), the tool degrades to plain-text
//   extraction via stdlib HTMLParser in fetch.py (still returns valid
//   Markdown-ish content with format="text-degraded" in the result), and
//   the `import { defineTool } from '@deepseek-ai/dsh-tools'` line throws
//   `ERR_MODULE_NOT_FOUND` (the plugin fails to load).

import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { defineTool } from '@deepseek-ai/dsh-tools'

const exec = promisify(execFile)
const HERE = dirname(fileURLToPath(import.meta.url))

const DEFAULT_MAX_BYTES = 500_000
const HARD_MAX_BYTES = 2_000_000
const HARD_MIN_BYTES = 5_000
const DEFAULT_TIMEOUT_MS = 25_000  // subprocess timeout; fetch.py default is 15s

// Pick the Python interpreter. Resolution order:
//   1. `DSH_WEB_FETCH_PYTHON` env var (power-user override; e.g. `py` on
//      Windows, `python3.12`, or an absolute venv path).
//   2. A per-user venv at `$DSH_HOME/venv/{bin/python,Scripts/python.exe}`
//      when present. Used to keep the plugin isolated from Homebrew's
//      PEP 668 marker without requiring shell env config; create with
//      `python3 -m venv ~/.dsh/venv` + `pip install extruct brotli`.
//   3. A per-user venv at `~/.dsh/venv/...` when `$DSH_HOME` is unset or
//      points elsewhere. Probed in addition to (2) so the patch keeps
//      working when the user has a custom `DSH_HOME`.
//   4. System `python` (Windows) / `python3` (POSIX) as a last resort.
// Cross-platform: `Scripts\python.exe` on win32, `bin/python` on macOS/Linux.
const _venvPyParts = process.platform === 'win32'
  ? ['venv', 'Scripts', 'python.exe']
  : ['venv', 'bin', 'python']
const _venvPyCandidates = [process.env.DSH_HOME, join(homedir(), '.dsh')]
  .filter(Boolean)
  .map((h) => join(h, ..._venvPyParts))
const _venvPy = _venvPyCandidates.find((p) => existsSync(p))
const PYTHON_BIN = process.env.DSH_WEB_FETCH_PYTHON
  || _venvPy
  || (process.platform === 'win32' ? 'python' : 'python3')

// Chars of the markdown body shipped inline. Full markdown lives in cache.
const PREVIEW_CHARS = 2_000

// ---- HTML → Markdown (Turndown + GFM) -----------------------------------
//
// We require lazily so a missing junction (and therefore a missing turndown
// install) downgrades to `format: 'text-degraded'` instead of crashing the
// whole plugin at load time.

let _turndown = null  // null when Turndown is unavailable
let _html2mdError = ''

async function buildTurndown() {
  if (_turndown !== null || _html2mdError !== '') return _turndown
  try {
    const TurndownService = (await import('turndown')).default
    const { gfm } = await import('@joplin/turndown-plugin-gfm')
    const td = new TurndownService({
      headingStyle: 'atx',
      codeBlockStyle: 'fenced',
      bulletListMarker: '-',
    })
    td.use(gfm)
    // Strip non-content tags wholesale (Turndown's default keeps their text).
    td.remove(['script', 'style', 'noscript', 'nav', 'header', 'footer', 'aside', 'form'])
    // Custom table cell with alignment preservation (mirror of shipped
    // dsh-tool-web's tableCellWithoutSpanExpansion rule).
    td.addRule('preserveTableAlignment', {
      filter: ['th', 'td'],
      replacement(content, node) {
        const align = (node.getAttribute('align') || node.style?.textAlign || '').toLowerCase()
        const text = content.trim().replace(/\n+/g, ' ').replace(/\|+/g, '\\|')
        return ` ${text} `
      },
    })
    _turndown = td
    return _turndown
  } catch (e) {
    _html2mdError = e.message
    return null
  }
}

// Note: the dynamic-import above uses `await import(...)`. The plugin
// registration in `apply()` cannot be async, so buildTurndown caches its
// outcome on first call and returns a TurndownService | null synchronously
// after that. The plugin therefore degrades gracefully on a missing
// junction: text-degraded mode instead of a load-time crash.

// ---- cache file layout --------------------------------------------------

const SECTION_HEADER = {
  resources: '## Resources (',
  metadata: '## Structured Data (',
}

function _countLines(s) {
  if (!s) return 0
  return s.split('\n').length - (s.endsWith('\n') ? 1 : 0)
}

// Rewrite the cache file Python wrote (raw body) into Markdown + Resources
// + Metadata. Returns { mdBody, mdResources, mdMetadata, bodyLines, ... }.
async function buildMarkdownAndRewriteCache(rawBody, metadata, links, images, title, url, cachePath) {
  const td = await buildTurndown()
  let mdBody = rawBody
  let format = 'text-degraded'
  let engine = 'htmlparser-fallback'
  if (td !== null) {
    try {
      mdBody = td.turndown(rawBody)
      format = 'markdown'
      engine = 'turndown+gfm'
    } catch (e) {
      mdBody = rawBody
      format = 'text-degraded'
      engine = 'htmlparser-fallback'
    }
  }

  // [v2 deprecated] Resources section disabled (see comment in execute()).
  // _renderResources() is retained for future re-enable.
  const mdResources = _renderResources(links, images) // returns '' when links/images are empty
  const mdMetadata = _renderMetadata(metadata)

  // Layout: <body>\n\n<metadata>  (resources section omitted)
  const sections = []
  sections.push(mdBody)
  // if (mdResources) sections.push(mdResources)
  if (mdMetadata) sections.push(mdMetadata)
  const fullText = sections.join('\n\n')

  // Write cache file (overwrite Python's raw-body version).
  if (cachePath) {
    try {
      const { writeFile } = await import('node:fs/promises')
      await writeFile(cachePath, fullText, 'utf8')
    } catch {
      // Cache write failed; preview still works from in-memory fullText.
    }
  }

  // Compute line layout for view_range hints.
  const bodyLines = _countLines(mdBody)
  const totalText = fullText
  const totalLines = _countLines(totalText)
  let resourcesStartLine = null
  let resourcesLines = 0
  let metadataStartLine = null
  let metadataLines = 0
  if (mdMetadata) {
    metadataStartLine = _findSectionStart(totalText, SECTION_HEADER.metadata)
    if (metadataStartLine !== null) {
      metadataLines = totalLines - metadataStartLine + 1
    }
  }
  if (mdResources) {
    resourcesStartLine = _findSectionStart(totalText, SECTION_HEADER.resources)
    if (resourcesStartLine !== null) {
      // Resources section ends right before metadata's blank-line separator,
      // or at the end of the file when no metadata section follows.
      const endBoundary = metadataStartLine !== null
        ? metadataStartLine - 1   // -1 for the blank line between sections
        : totalLines
      resourcesLines = endBoundary - resourcesStartLine + 1
    }
  }

  return {
    mdBody, fullText, format, engine,
    bodyLines, totalLines,
    resourcesStartLine, resourcesLines,
    metadataStartLine, metadataLines,
  }
}

function _renderResources(links, images) {
  const total = (links?.length || 0) + (images?.length || 0)
  if (total === 0) return ''
  const lines = [`## Resources (${links?.length || 0} links, ${images?.length || 0} images)`, '']
  if (links?.length) {
    lines.push('### Links', '')
    for (const [text, linkUrl] of links.slice(0, 200)) {
      lines.push(`- [${text || linkUrl}](${linkUrl})`)
    }
    lines.push('')
  }
  if (images?.length) {
    lines.push('### Images', '')
    for (const [alt, imgUrl] of images.slice(0, 200)) {
      lines.push(`- ![${alt}](${imgUrl})`)
    }
    lines.push('')
  }
  if (total > 200) {
    lines.push(`(... and ${total - 200} more)`, '')
  }
  return lines.join('\n')
}

function _renderMetadata(metadata) {
  if (!metadata || typeof metadata !== 'object') return ''
  // Trim noisy keys; keep human-meaningful schema types.
  const cleaned = {}
  for (const k of ['json-ld', 'microdata', 'opengraph', 'rdfa', 'microformat']) {
    if (Array.isArray(metadata[k]) && metadata[k].length > 0) {
      cleaned[k] = metadata[k]
    }
  }
  if (Object.keys(cleaned).length === 0) return ''
  const summary = []
  if (cleaned['json-ld']) summary.push(`${cleaned['json-ld'].length} JSON-LD`)
  if (cleaned['microdata']) summary.push(`${cleaned['microdata'].length} microdata`)
  if (cleaned['opengraph']) summary.push(`${cleaned['opengraph'].length} OpenGraph`)
  if (cleaned['rdfa']) summary.push(`${cleaned['rdfa'].length} RDFa`)
  if (cleaned['microformat']) summary.push(`${cleaned['microformat'].length} microformat`)
  const lines = [`## Structured Data (${summary.join(', ')})`, '']
  lines.push('```json')
  lines.push(JSON.stringify(cleaned, null, 2))
  lines.push('```')
  lines.push('')
  return lines.join('\n')
}

function _findSectionStart(text, headerPrefix) {
  // Returns 1-indexed line number where the section begins, or null.
  if (!text) return null
  const lines = text.split('\n')
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].startsWith(headerPrefix)) return i + 1
  }
  return null
}

// ---- links / images extraction (for Resources section) ------------------
//
// Turndown strips <a>/<img> tags' href/src from the Markdown body when it
// can convert them inline (links become [text](url), images become
// ![alt](url)). We still want a Resources section so the model can list
// every link/image at a glance. We do this with a tiny custom Turndown
// rule that appends to a side list instead of replacing the node.
//
// NOTE: we currently use a simpler approach: parse the HTML once for
// <a>/<img> attrs using regex (we already have the raw HTML from Python).
// This is faster and avoids mutating the Turndown service.

function extractLinksAndImages(rawHtml, baseUrl) {
  const links = []
  const images = []
  const seenLinks = new Set()
  const seenImages = new Set()

  // Match <a ... href="..." ...>...</a> (greedy across attrs, lazy in body).
  const linkRe = /<a\b[^>]*?\bhref\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))[^>]*>([\s\S]*?)<\/a>/gi
  let m
  while ((m = linkRe.exec(rawHtml)) !== null) {
    const href = (m[1] || m[2] || m[3] || '').trim()
    if (!href) continue
    if (/^(?:mailto|javascript|data|tel):/i.test(href)) continue
    const absUrl = absolutize(href, baseUrl)
    if (seenLinks.has(absUrl)) continue
    seenLinks.add(absUrl)
    const innerText = stripTags((m[4] || '').trim()).slice(0, 80)
    links.push([innerText || absUrl, absUrl])
    if (links.length >= 500) break
  }

  // Match <img ... src="..." ...> (self-closing OK).
  const imgRe = /<img\b[^>]*?\bsrc\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))[^>]*>/gi
  while ((m = imgRe.exec(rawHtml)) !== null) {
    const src = (m[1] || m[2] || m[3] || '').trim()
    if (!src) continue
    if (/^(?:data|javascript):/i.test(src)) continue
    const absUrl = absolutize(src, baseUrl)
    if (seenImages.has(absUrl)) continue
    seenImages.add(absUrl)
    const altMatch = rawHtml.slice(Math.max(0, m.index - 0), m.index + m[0].length).match(/\balt\s*=\s*(?:\"([^\"]*)\"|'([^']*)')/i)
    const alt = (altMatch ? (altMatch[1] || altMatch[2] || '') : '').trim()
    images.push([alt, absUrl])
    if (images.length >= 500) break
  }

  return { links, images }
}

function absolutize(url, baseUrl) {
  if (!baseUrl) return url
  if (url.startsWith('#') || url.startsWith('?')) return url
  try {
    return new URL(url, baseUrl).toString()
  } catch {
    return url
  }
}

function stripTags(s) {
  return s.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim()
}

// ---- tool registration --------------------------------------------------

export const name = 'web-fetch-local'
export const inject = ['tools']

export function apply(ctx) {
  ctx.tools.register(defineTool({
    name: 'web_fetch',

    description:
      'Fetch the text content of an HTTP(S) URL. Returns a short inline preview ' +
      '(first ~2000 chars of Markdown) and the path to a cache file with the full ' +
      'extracted content (Markdown body + Resources + Structured Data). **The ' +
      'inline preview is truncated** — read the cache file at the indicated path ' +
      'to see the complete page (use whichever file-reading tool is available).',

    parameters: {
      url: {
        type: 'string',
        required: true,
        description: 'Absolute http(s) URL to fetch.',
      },
      // max_bytes removed from parameters — LLM no longer controls byte cap.
      // Cap is hardcoded to DEFAULT_MAX_BYTES below (mirrors web_search's
      // RESULT_LIMIT=8 philosophy: only expose what the agent needs to know).
    },

    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          ok: { type: 'boolean' },
          title: { type: 'string' },
          url: { type: 'string' },
          statusCode: { type: 'number' },
          bytes: { type: 'number' },
          truncated: { type: 'boolean' },
          contentType: { type: 'string' },
          format: { type: 'string' },                        // 'markdown' | 'text-degraded'
          htmlToMdEngine: { type: 'string' },                // 'turndown+gfm' | 'htmlparser-fallback'
          metadata: { oneOf: [{ type: 'object', additionalProperties: true }, { type: 'null' }] },   // JSON-LD/OG/Microdata/RDFa; null when absent
          metadataKind: { type: 'string' },                  // 'json-ld'|'opengraph'|'mixed'|'none'
          extructAvailable: { type: 'boolean' },
          textChars: { type: 'number' },
          previewChars: { type: 'number' },
          bodyLines: { type: 'number' },
          resourcesLines: { type: 'number' },
          resourcesStartLine: { oneOf: [{ type: 'number' }, { type: 'null' }] },  // null when no resources
          metadataLines: { type: 'number' },
          metadataStartLine: { oneOf: [{ type: 'number' }, { type: 'null' }] },   // null when no metadata
          totalBytes: { oneOf: [{ type: 'number' }, { type: 'null' }] },         // null when no Content-Length
          totalLines: { type: 'number' },
          linksCount: { type: 'number' },
          imagesCount: { type: 'number' },
          fullPath: { type: 'string' },
          preview: { type: 'string' },
          error: { type: 'string' },
        },
      },
      render(_args, value) {
        if (!value.ok) {
          return [{
            type: 'text',
            text: `${value.url || '(no url)'} (${value.statusCode ? 'HTTP ' + value.statusCode : 'ERR'})\nError: ${value.error || ''}`
          }]
        }
        const titleLine = value.title ? `Title: ${value.title}\n` : ''
        const head = `${value.url} (HTTP ${value.statusCode}, ${value.bytes} bytes${value.truncated ? ', truncated' : ', complete'})`
        const formatTag = value.format === 'markdown'
          ? `[format=${value.format}; engine=${value.htmlToMdEngine}]`
          : `[format=${value.format} — install turndown junction for Markdown]`
        const metaTag = value.metadataKind && value.metadataKind !== 'none'
          ? ` [metadata=${value.metadataKind}]`
          : ''
        const layoutLines = []
        layoutLines.push(`Body: ${value.textChars} chars, lines 1-${value.bodyLines}`)
        // [v2 deprecated] Resources section disabled — no Resources summary line.
        // if (value.resourcesStartLine !== null) {
        //   layoutLines.push(`Resources: ${value.linksCount} links + ${value.imagesCount} images, lines ${value.resourcesStartLine}-${value.resourcesStartLine + value.resourcesLines - 1}`)
        // }
        if (value.metadataStartLine !== null) {
          layoutLines.push(`Metadata: ${value.metadataKind}, lines ${value.metadataStartLine}-${value.metadataStartLine + value.metadataLines - 1}`)
        }
        const cacheHints = value.fullPath
          ? `\n\n${layoutLines.join('\n')}\n\n` +
            (value.bodyLines > 0
              ? `Read body:      ${value.fullPath} lines 1-${value.bodyLines}\n`
              : '') +
            // [v2 deprecated] Resources section disabled — no "Read resources" hint.
            // (value.resourcesStartLine !== null
            //   ? `Read resources: ${value.fullPath} lines ${value.resourcesStartLine}-${value.resourcesStartLine + value.resourcesLines - 1}\n`
            //   : '') +
            (value.metadataStartLine !== null
              ? `Read metadata:  ${value.fullPath} lines ${value.metadataStartLine}-${value.metadataStartLine + value.metadataLines - 1}\n`
              : '')
            // [v2 deprecated] Read whole is redundant (= body + metadata).
            // Removed for tool simplicity. To restore, add back:
            // + `Read whole:     str_replace_editor(command="view", path="${value.fullPath}", view_range=[1, -1])`
          : ''
        return [{
          type: 'text',
          text: `${titleLine}${head} ${formatTag}${metaTag}\n\n${value.preview}${cacheHints}`
        }]
      },
    },

    async execute(args, execCtx) {
      const url = String(args.url ?? '').trim()
      if (!url) throw new Error('url must be a non-empty string')
      if (!/^https?:\/\//i.test(url)) throw new Error('url must start with http:// or https://')

      // Byte cap is fixed (LLM cannot tune). 500KB covers ~99% of pages;
      // for the rare oversized page the cache file still gets the full
      // DEFAULT_MAX_BYTES worth of body and the inline preview is truncated.
      const maxBytes = DEFAULT_MAX_BYTES

      const scriptPath = join(HERE, 'fetch.py')
      const cmdArgs = [
        scriptPath,
        '--url', url,
        '--max-bytes', String(maxBytes),
        '--timeout', '15',
      ]

      let pyResult
      try {
        const { stdout } = await exec(PYTHON_BIN, cmdArgs, {
          timeout: DEFAULT_TIMEOUT_MS,
          maxBuffer: 16 * 1024 * 1024,
          signal: execCtx.signal,
          windowsHide: true,
        })
        try {
          pyResult = JSON.parse(stdout)
        } catch (e) {
          throw new Error(`web_fetch: fetch.py returned non-JSON: ${e.message}\nstdout=${stdout.slice(0, 500)}`)
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

      if (!pyResult.ok) {
        return {
          ok: false,
          url: pyResult.url || url,
          statusCode: pyResult.status || 0,
          bytes: pyResult.bytes || 0,
          contentType: pyResult.content_type || '',
          error: pyResult.error || 'fetch failed',
        }
      }

      const body = pyResult.body || ''
      const bodyKind = pyResult.body_kind || 'text'
      const metadata = pyResult.metadata || null
      const finalUrl = pyResult.url || url

      // For HTML pages, extract links + images for the Resources section,
      // then turn the body into Markdown via Turndown. Non-HTML bodies pass
      // through verbatim (text/json/markdown).
      let layout = {
        mdBody: body, fullText: body, format: 'text-degraded',
        engine: 'htmlparser-fallback',
        bodyLines: _countLines(body), totalLines: _countLines(body),
        resourcesStartLine: null, resourcesLines: 0,
        metadataStartLine: null, metadataLines: 0,
      }
      let links = []
      let images = []
      if (bodyKind === 'html') {
        // [v2 deprecated] Resources section disabled — Markdown body already
        // contains every link and image as [text](url) / ![alt](url); a
        // separate Resources section was 100% redundant and crowded the
        // model's context. extractLinksAndImages() is retained below for
        // future re-enable. To restore, uncomment the block below and the
        // corresponding Resources writer in buildMarkdownAndRewriteCache().
        // const extracted = extractLinksAndImages(body, finalUrl)
        // links = extracted.links
        // images = extracted.images
        layout = await buildMarkdownAndRewriteCache(
          body, metadata, links, images,
          pyResult.title || '', finalUrl, pyResult.full_path || ''
        )
      } else {
        // Non-HTML: still expose structured metadata when present.
        const mdMetadata = _renderMetadata(metadata)
        if (mdMetadata) {
          layout.fullText = `${body}\n\n${mdMetadata}`
          layout.totalLines = _countLines(layout.fullText)
          layout.metadataStartLine = _findSectionStart(layout.fullText, SECTION_HEADER.metadata)
          layout.metadataLines = layout.totalLines - (layout.metadataStartLine || 0) + 1
        }
      }

      const preview = layout.fullText.slice(0, PREVIEW_CHARS)

      return {
        ok: true,
        title: pyResult.title || '',
        url: finalUrl,
        statusCode: pyResult.status || 0,
        bytes: pyResult.bytes || 0,
        totalBytes: pyResult.total_bytes || null,
        truncated: !!pyResult.truncated,
        contentType: pyResult.content_type || '',
        format: layout.format,
        htmlToMdEngine: layout.engine,
        metadata: metadata,
        metadataKind: pyResult.metadata_kind || 'none',
        extructAvailable: !!pyResult.extruct_available,
        textChars: layout.fullText.length,
        previewChars: preview.length,
        bodyLines: layout.bodyLines,
        // [v2 deprecated] Resources section disabled — fields retained for
        // schema compatibility but always 0 / null.
        resourcesLines: 0,
        resourcesStartLine: null,
        metadataLines: layout.metadataLines,
        metadataStartLine: layout.metadataStartLine,
        totalLines: layout.totalLines,
        linksCount: 0,
        imagesCount: 0,
        fullPath: pyResult.full_path || '',
        preview,
        error: pyResult.cache_error || '',
      }
    },
  }))
}
