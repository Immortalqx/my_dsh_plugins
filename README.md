# my_dsh_plugins

Local `web_search` (via `mmx` CLI, MiniMax OAuth) and `web_fetch` (Python urllib + Node Turndown) plugins for DSH, scoped to the `standard-mmx` agent preset only.

## Install

```bash
python install.py
```

Writes three locations under `~/.dsh/`:

- `~/.dsh/plugins/web-search-mmx/`
- `~/.dsh/plugins/web-fetch-local/`
- `~/.dsh/.agent-presets/standard-mmx/`

Does **not** touch `~/.dsh/profiles/web/`, `~/.dsh/settings.yaml`, or any host-scope config. Requires Python ≥ 3.10. After install, the repo is no longer needed at runtime.

Flags:

- `--dry-run` — print the plan without writing anything
- `--source <dir>` — repo root (default: this script's parent directory)
- `--target-home <dir>` — override `$HOME` (for testing)

### Recommended: per-user venv for `web_fetch`

`web_fetch` soft-imports `extruct` and `brotli`. The recommended home for them is a dedicated venv at `~/.dsh/venv/` — the plugin auto-discovers it on load, no env var needed.

```bash
python3 -m venv ~/.dsh/venv
~/.dsh/venv/bin/python -m pip install extruct brotli
```

When `$DSH_HOME` is set, create the venv there instead. On PEP 668 distros (Homebrew Python 3.12+, Debian Bookworm+) this also avoids `pip install` being rejected for the system interpreter. Without a venv the plugin falls back to system `python3`; `extruct` / `brotli` degrade gracefully (null metadata / br-decode error).

## Use

1. Refresh the browser (F5) to reload plugins.
2. Settings → Agent preset → custom → pick **"Standard (mmx)"**.
3. Call `web_search` and `web_fetch` from chat like any other DSH tool — they appear as native tools to the model.

## What you get

### `web_search`

Up to 8 sources per call, sourced via the `mmx` CLI:

```ts
{
  sources: [{ url, title?, snippet?, publishedAt? }],
  truncated: boolean,
}
```

The model-facing output includes guidance: "the snippet is a brief excerpt, not full content" and "`web_fetch` reads the complete page from a URL". URLs must be cited as Markdown links in any answer.

### `web_fetch`

Returns a Markdown rendering of the page plus extracted structured data:

```ts
{
  ok: boolean,
  title: string,
  url: string,                    // final URL after redirects
  statusCode: number,
  bytes: number,
  totalBytes: number | null,
  truncated: boolean,
  contentType: string,
  format: "markdown" | "text-degraded",
  htmlToMdEngine: "turndown+gfm" | "htmlparser-fallback",
  metadata: object | null,        // { "json-ld": [...], "opengraph": [...], ... }
  metadataKind: "json-ld" | "opengraph" | "mixed" | "none",
  extructAvailable: boolean,
  textChars: number,
  previewChars: number,
  bodyLines: number,
  metadataLines: number,
  metadataStartLine: number | null,
  totalLines: number,
  fullPath: string,               // path to the cache file
  preview: string,                // first ~2000 chars of the cache file
  error: string,
}
```

The first ~2000 chars of the Markdown body are returned inline. The full content (body + structured data) lives in the cache file at `~/.dsh/cache/web-fetch/web-fetch_<url-hash8>.txt`, and the model-facing output gives line-number ranges for body and metadata. Example rendered output:

```
Title: ...
URL (HTTP 200, NNN bytes, complete) [format=markdown; engine=turndown+gfm] [metadata=opengraph]

[first 2000 chars of markdown]

Body: NNNN chars, lines 1-N
Metadata: opengraph, lines X-Y

Read body:      /Users/.../web-fetch_4d163095.txt lines 1-N
Read metadata:  /Users/.../web-fetch_4d163095.txt lines X-Y
```

## How this differs from DSH defaults

| | `standard-mmx` (this plugin) | DSH default |
|---|---|---|
| `web_search` | `mmx` CLI (MiniMax OAuth), up to 8 sources | `@deepseek-ai/dsh-web-search-deepseek` |
| `web_fetch` | Local Python urllib + Node Turndown; structured-data extraction | `@deepseek-ai/dsh-tool-web` |

The `standard-mmx` preset registers the local tools and disables the shipped ones (`@deepseek-ai/dsh-web-search-deepseek`, `@deepseek-ai/dsh-tool-web`) **only inside the preset's sessions**. Other presets and default sessions continue to use DSH's official tools. The host-scope config under `~/.dsh/profiles/web/` and `~/.dsh/settings.yaml` is untouched.

`web_fetch`'s output is richer than the shipped tool's plain-text result: structured data (JSON-LD, OpenGraph, Microdata, RDFa, Microformat) is extracted when `extruct` is installed, useful for pages with schema.org metadata.

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `DSH_WEB_FETCH_PYTHON` | `python` on Windows, `python3` elsewhere | Override the Python interpreter (power-user override) |
| `DSH_WEB_SEARCH_MMX_BIN` | `mmx` on PATH | Override the mmx binary path |
| `DSH_HOME` | `~/.dsh` | Override the DSH home |

## Verify

```bash
ls ~/.dsh/plugins/web-fetch-local ~/.dsh/plugins/web-search-mmx
ls ~/.dsh/.agent-presets/standard-mmx
node -e "console.log(require('turndown/package.json').version)"  # run from ~/.dsh/plugins/web-fetch-local
```

## Prerequisites

- DSH ≥ 0.1.0-rc.6, Node ≥ 18.0.0
- `install.py`: Python ≥ 3.10
- `web-search-mmx`: `mmx` CLI on `PATH`, authenticated via `mmx auth login`
- `web-fetch-local`: Python ≥ 3.10; optional `extruct` (structured data) and `brotli` (br decoding) — see Install → venv

## Layout

This repo (install source):

```
my_dsh_plugins/
- README.md
- install.py
- presets/standard-mmx/{agent.cordis.yml, preset.yml}
- web-search-mmx/{package.json, src/index.mjs}
- web-fetch-local/{package.json, src/{index.mjs, fetch.py}}
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
