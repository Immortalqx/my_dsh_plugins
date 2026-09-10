# my_dsh_plugins

Local `web_search` (via `mmx` CLI, MiniMax OAuth) and `web_fetch` (Python urllib + Node Turndown) plugins for DSH, scoped to the `standard-mmx` agent preset only.

Validated against **`@deepseek-ai/dsh` 0.1.5-rc.1**. Each DSH release changes the preset and tool-schema contracts this repo depends on, so a tree that works on one release can fail to mount on the next — see [Versioning](#versioning) for which tag to check out.

## Install

```bash
python install.py
```

Writes three locations under `~/.dsh/`:

- `~/.dsh/plugins/web-search-mmx/`
- `~/.dsh/plugins/web-fetch-local/`
- `~/.dsh/.agent-presets/standard-mmx/`

then runs `tools/verify-contract.mjs` against what it just wrote and reports the result. Does **not** touch `~/.dsh/profiles/web/`, `~/.dsh/settings.yaml`, or any host-scope config. Requires Python ≥ 3.10. After install, the repo is no longer needed at runtime.

Flags:

- `--dry-run` — print the plan without writing anything
- `--no-verify` — skip the post-install contract probe
- `--source <dir>` — repo root (default: this script's parent directory)
- `--target-home <dir>` — override `$HOME` (for testing against a throwaway home)

### Recommended: per-user venv for `web_fetch`

`web_fetch` soft-imports `extruct` and `brotli`. The recommended home for them is a dedicated venv at `~/.dsh/venv/` — the plugin auto-discovers it on load, no env var needed.

```bash
python3 -m venv ~/.dsh/venv
~/.dsh/venv/bin/python -m pip install extruct brotli
```

When `$DSH_HOME` is set, create the venv there instead. On PEP 668 distros (Homebrew Python 3.12+, Debian Bookworm+) this also avoids `pip install` being rejected for the system interpreter. Without a venv the plugin falls back to system `python3`; `extruct` / `brotli` degrade gracefully (null metadata / br-decode error).

## Use

1. Refresh the browser (F5) to reload plugins.
2. Settings → Agent preset → pick **"Standard (mmx)"**.
3. Call `web_search` and `web_fetch` from chat like any other DSH tool — they appear as native tools to the model.

## Upgrading after a new DSH release

Two things drift when DSH updates, and both are checked into this repo:

1. **The preset** is a copy of the `standard` composition DSH ships, which changes every release (rows are added, config keys renamed — 0.1.5 turned the persona row's `text:` into `prefix:`/`suffix:`, which fails a stale copy at mount). Re-derive it:

   ```bash
   python tools/rebase_preset.py            # regenerate from the freshly installed DSH
   python tools/rebase_preset.py --check    # exit 1 when the checked-in copy is stale
   git diff presets/                       # review what upstream changed
   ```

2. **The tool-schema contract** is enforced by `@deepseek-ai/dsh-tools`, whose `defineTool()` compiler is strict and versioned. Re-verify both plugins:

   ```bash
   python install.py            # installs, then runs the probe automatically
   node tools/verify-contract.mjs --smoke   # probe + live execution of both tools
   ```

Both tools are deliberately self-describing: `tools/rebase_preset.py` re-applies the preset deltas and fails loudly when upstream no longer has the shape it edits, and `tools/verify-contract.mjs` asserts the compiled model-facing schema, not just that the module loads.

## What you get

### `web_search`

Arguments mirror DSH 0.1.5's shipped `web_search`: **`{ queries: string[] }`**, 1–4 non-empty queries. Queries run concurrently, exact duplicates collapse, and their sources merge round-robin (one per query, then the next round) into at most 8 results, deduplicated by URL.

```ts
{
  sources: [{ url, title?, snippet?, publishedAt? }],
  truncated: boolean,     // true when the 8-result cap dropped sources
  queriesRun: number,     // after duplicate collapse
  errors: string[],       // one entry per query that failed
}
```

The model-facing output includes guidance: "the snippet is a brief excerpt, not full content" and "`web_fetch` reads the complete page from a URL". URLs must be cited as Markdown links in any answer.

### `web_fetch`

Arguments are unchanged (`{ url }`). Returns a Markdown rendering of the page plus extracted structured data:

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

| | `standard-mmx` (this repo) | DSH 0.1.5 default |
|---|---|---|
| `web_search` backend | `mmx` CLI (MiniMax OAuth) | `@deepseek-ai/dsh-web-search-deepseek` (`DEEPSEEK_API_KEY`) |
| `web_search` arguments | `queries: string[]`, 1–4 | `queries: string[]`, 1–4 |
| `web_search` provider answer | none — mmx returns organic results only | optional `answer` from the provider |
| `web_search` partial failure | successful queries' sources are returned, failures listed in `errors` | whole call fails, successful results discarded |
| `web_fetch` backend | local Python urllib + Node Turndown | `@deepseek-ai/dsh-web-fetch-http` |
| `web_fetch` result | ~2000-char preview + full page in a cache file with line ranges | truncated plain text |
| Structured data | JSON-LD / OpenGraph / Microdata / RDFa / Microformat via `extruct` | not extracted |

The `standard-mmx` preset registers the local tools and disables the shipped `@deepseek-ai/dsh-tool-web` row (which provides both `web_search` and `web_fetch`) **only inside the preset's sessions**. Other presets and default sessions continue to use DSH's official tools. The host-scope config under `~/.dsh/profiles/web/` and `~/.dsh/settings.yaml` is untouched.

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `DSH_WEB_FETCH_PYTHON` | `python` on Windows, `python3` elsewhere | Override the Python interpreter (power-user override) |
| `DSH_WEB_SEARCH_MMX_BIN` | `mmx` on PATH | Override the mmx binary path. On Windows the binary is always launched through `cmd.exe`, so a `.cmd`/`.bat` shim works |
| `DSH_HOME` | `~/.dsh` | Override the DSH home |

## Verify

```bash
node tools/verify-contract.mjs            # schema/contract checks on the installed plugins
node tools/verify-contract.mjs --smoke    # …and execute both tools for real
node tools/verify-contract.mjs --dsh-home <dir>   # verify a --target-home install
python tools/rebase_preset.py --check     # is the preset still in sync with the installed DSH?
```

`verify-contract.mjs` loads each plugin from its installed location (that is where its `@deepseek-ai/dsh-tools` import resolves through the junction), compiles its tool definition with the installed compiler, and checks the resulting model-facing `parameters` schema, argument rejection, `output.schema`, and `render()`. `--smoke` additionally runs both tools once and validates the live result against the output schema.

## Prerequisites

- DSH **0.1.5-rc.1** (the preset is derived from that release's shipped `standard` composition)
- Node ≥ 18.0.0
- `install.py` / `tools/rebase_preset.py`: Python ≥ 3.10
- `web-search-mmx`: `mmx` CLI on `PATH`, authenticated via `mmx auth login`
- `web-fetch-local`: Python ≥ 3.10; optional `extruct` (structured data) and `brotli` (br decoding) — see Install → venv

## Versioning

This repo carries no single version number — its two plugin packages version independently, and what actually matters is which DSH release the tree was validated against. Tags name exactly that:

| Tag | DSH release | `web_search` arguments |
|---|---|---|
| `dsh-0.1.0-rc.7` | 0.1.0-rc.7 | `query: string` |
| `dsh-0.1.5-rc.1` | 0.1.5-rc.1 | `queries: string[]` |

`main` tracks the newest supported release and matches the newest tag above. To run an older tree against the DSH release it belongs to:

```bash
git checkout dsh-0.1.0-rc.7
python install.py
```

## Layout

This repo (install source):

```
my_dsh_plugins/
- README.md
- install.py
- presets/standard-mmx/{agent.cordis.yml, preset.yml}   <- agent.cordis.yml is generated
- tools/rebase_preset.py                                <- regenerates the preset from DSH
- tools/verify-contract.mjs                             <- re-checks the tool schemas
- web-search-mmx/{package.json, src/index.mjs}
- web-fetch-local/{package.json, src/{index.mjs, fetch.py}}
```

After install (DSH-managed):

```
~/.dsh/plugins/
- web-search-mmx/...
- web-fetch-local/
    - node_modules/                       <- junction to the DSH package's node_modules
    - src/...
~/.dsh/.agent-presets/standard-mmx/
~/.dsh/cache/web-fetch/                  <- created on first web_fetch call
```
