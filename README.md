# my_dsh_plugins

`web_search` (mmx-cli) + `web_fetch` (Python urllib + Node Turndown) plugins for DSH, scoped to the `standard-mmx` agent preset only.

## Install

```bash
python install.py
```

`install.py` writes three locations under `~/.dsh/` and nowhere else:

- `~/.dsh/plugins/web-search-mmx/`
- `~/.dsh/plugins/web-fetch-local/`
- `~/.dsh/.agent-presets/standard-mmx/`

It does **not** touch `~/.dsh/profiles/web/`, `~/.dsh/settings.yaml`, or any other host-scope config. The official DSH modes (`@deepseek-ai/dsh-web-search-deepseek`, `@deepseek-ai/dsh-tool-web`) remain active for everyone; the `standard-mmx` preset overrides them locally for its own sessions only.

Flags:

- `--dry-run` — print the plan without writing anything
- `--source <dir>` — repo root (default: this script's parent directory)
- `--target-home <dir>` — override `$HOME` (for testing)

Requirements: Python ≥ 3.10 (no other deps required to install — see below for optional ones).

After the install, the plugin repo is no longer needed at runtime.

In the browser, F5 then Settings → Agent preset → custom → pick "Standard (mmx)".

## `web_search` (web-search-mmx)

Backed by the local `mmx` CLI (MiniMax OAuth). Returns up to 8 sources per call:

```ts
{
  sources: [{ url, title?, snippet?, publishedAt? }],
  truncated: boolean,
}
```

Override the mmx binary path with `DSH_WEB_SEARCH_MMX_BIN=<path>`.

## `web_fetch` (web-fetch-local) — v2

v2 is a two-stage pipeline. Python does HTTP + SSRF + raw HTML preservation + JSON-LD extraction; Node does HTML → Markdown via Turndown (reusing DSH's bundled `turndown` + `@joplin/turndown-plugin-gfm`).

```
[Node] index.mjs ─→ spawn Python ─→ [Python] fetch.py
  ↑                                       ↓
  └─ Turndown.turndown(rawHtml)           urllib + SSRF gate
     ↓                                   gzip / deflate / br decode
  rewrite cache file                     extruct → JSON-LD / OG / Microdata / RDFa
  ↓
  Markdown body + Resources + Metadata
```

### What you get

- **Markdown body** (Turndown + GFM): tables, code blocks, links, image refs preserved
- **Resources section**: every `<a href>` and `<img src>` collected with absolute URLs
- **Structured Data section**: JSON-LD, Microdata, OpenGraph, RDFa, Microformat extracted by `extruct` (when installed) and rendered as a fenced JSON code block

### SSRF defense (unchanged from v1)

Any public host is allowed; only loopback / private suffixes are blocked:

```
localhost / 127.0.0.1 / ::1 / 0.0.0.0 / 0    (loopback)
*.local / *.internal / *.lan                  (private suffixes)
literal IPv4/IPv6 in those ranges
```

To add a custom blocklist rule, edit `PRIVATE_HOSTS` or `PRIVATE_SUFFIXES` in `web-fetch-local/src/fetch.py`. DSH HMR picks up the change on next `web_fetch` call.

### Optional Python deps

| Dep | What it adds | Install |
|---|---|---|
| `extruct` | JSON-LD, Microdata, OpenGraph, RDFa, Microformat extraction | `pip install extruct` |
| `brotli` | `Content-Encoding: br` decoding (rare) | `pip install brotli` |

Both are soft-imported at runtime. Without `extruct`, `metadata` in the result is `null` and `metadataKind` is `"none"` — the tool still works for plain Markdown extraction. Without `brotli`, a br-encoded response is rejected with a friendly error.

### Node bridge

`index.mjs` needs `turndown` and `@joplin/turndown-plugin-gfm`. DSH already ships them as deps of `@deepseek-ai/dsh`, but Node module resolution walks up from the plugin's `index.mjs` looking for a `node_modules/` directory and cannot see into nested `@deepseek-ai/dsh/node_modules/`. `install.py` solves this by creating a directory junction:

```
~/.dsh/plugins/web-fetch-local/node_modules/
    -> <DSH root>/node_modules/@deepseek-ai/dsh/node_modules/
```

On Windows this uses `mklink /J` (no admin required); on POSIX it uses `os.symlink`. If the junction is missing, `install.py` logs a note and the plugin degrades to plain-text extraction (the cache file gets raw HTML instead of Markdown and the result reports `format: "text-degraded"`).

### Result schema

```ts
{
  ok: boolean,
  title: string,
  url: string,                    // final URL after redirects
  statusCode: number,
  bytes: number,
  totalBytes: number | null,
  truncated: boolean,
  contentType: string,            // mime type stripped of params

  // v2 additions:
  format: "markdown" | "text-degraded",
  htmlToMdEngine: "turndown+gfm" | "htmlparser-fallback",
  metadata: object | null,        // { "json-ld": [...], "opengraph": [...], ... }
  metadataKind: "json-ld" | "opengraph" | "mixed" | "none",
  extructAvailable: boolean,

  textChars: number,
  previewChars: number,
  bodyLines: number,
  resourcesLines: number,
  resourcesStartLine: number | null,
  metadataLines: number,
  metadataStartLine: number | null,
  totalLines: number,
  linksCount: number,
  imagesCount: number,
  fullPath: string,               // path to the rewritten cache file
  preview: string,                // first ~2000 chars of the rewritten file
  error: string,
}
```

The model-facing `output.render` formats the result as:

```
Title: ...
URL (HTTP 200, NNN bytes, complete) [format=markdown; engine=turndown+gfm] [metadata=opengraph]

[first 2000 chars of markdown]

Body: NNNN chars, lines 1-N
Resources: N links + N images, lines X-Y
Metadata: opengraph, lines X-Y

Read body:      str_replace_editor(command="view", path="...", view_range=[1, N])
Read resources: str_replace_editor(command="view", path="...", view_range=[X, Y])
Read metadata:  str_replace_editor(command="view", path="...", view_range=[X, Y])
Read whole:     str_replace_editor(command="view", path="...", view_range=[1, -1])
```

### Cache layout

Cache files live at `~/.dsh/cache/web-fetch/web-fetch_<url-hash8>.txt`. v2 rewrites them from raw HTML (v1) to:

```
<markdown body>

## Resources (N links, M images)
### Links
- [text](url)
- ...
### Images
- ![alt](url)
- ...

## Structured Data (X JSON-LD, Y OpenGraph, ...)
```json
{ "json-ld": [...], "opengraph": [...], ... }
```
```

The same URL always maps to the same file, so repeated fetches overwrite in place.

### Configuration

| Env var | Default | Effect |
|---|---|---|
| `DSH_WEB_FETCH_PYTHON` | `python` on Windows, `python3` elsewhere | Override the Python interpreter |
| `DSH_HOME` | `~/.dsh` | Override the DSH home (mainly for tests) |

## Verify

```bash
ls ~/.dsh/plugins/web-fetch-local ~/.dsh/plugins/web-search-mmx
ls ~/.dsh/.agent-presets/standard-mmx
```

To confirm the Node bridge worked after install:

```bash
node -e "console.log(require('turndown/package.json').version)"  # run from ~/.dsh/plugins/web-fetch-local
```

## Layout

This clone (the install source):

```
my_dsh_plugins/
- README.md
- install.py
- presets/standard-mmx/{agent.cordis.yml, preset.yml}
- web-search-mmx/{package.json, src/index.mjs}
- web-fetch-local/{package.json, src/{index.mjs, fetch.py}, docs/RESEARCH-NOTES.md}
```

After install (DSH-managed):

```
~/.dsh/plugins/
- web-search-mmx/...
- web-fetch-local/
    - node_modules/                       <- junction to DSH's node_modules
    - src/...
~/.dsh/.agent-presets/standard-mmx/
~/.dsh/cache/web-fetch/                  <- created on first web_fetch call
```

## Compatibility

- DSH >=0.1.0-rc.6, Node >=18.0.0
- install.py: Python ≥ 3.10
- web-fetch-local: Python >=3.10, optional `extruct` and `brotli`
- web-search-mmx: `mmx` CLI on `PATH` (OAuth via `mmx auth login`)
