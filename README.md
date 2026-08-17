# my_dsh_plugins

`web_search` (mmx-cli) + `web_fetch` (Python urllib) plugins for DSH.

## Install

```bash
python install.py
```

`install.py` auto-detects the host OS, force-overwrites this plugin's files (idempotent), and merges its rows into the existing `~/.dsh/profiles/web/cordis.patch.yml` — rows that belong to *other* plugins are preserved.

Flags:
- `--dry-run` — print the plan without writing anything
- `--source <dir>` — repo root (default: this script's parent directory)
- `--target-home <dir>` — override `$HOME` (for testing)

Requirements: Python ≥ 3.10 + PyYAML (`pip install pyyaml`).

After the install, the plugin repo is no longer needed at runtime.

In the browser, F5 then Settings -> Agent preset -> custom -> pick "Standard (custom)".

## How `web_fetch` delivers long content

`web_fetch` returns a short inline preview (first ~2000 chars) plus the absolute path to a cache file that holds the full extracted text:

```
URL (HTTP 200, 125922 bytes, 43335 chars text, complete)

[preview: first ~2000 chars]

Full content (43335 chars) saved to: ~/.dsh/cache/web-fetch/web-fetch_<url-hash>.txt
Use the read tool (str_replace_editor, command=view) to access the complete text.
```

When the model needs the rest of the article (references, later sections, full text for a long page), it calls `str_replace_editor` with `command="view"`, `path=<fullPath>`, and **always** `view_range=[1, -1]`. The `view_range=[1, -1]` is required: without it, the read tool's default 16K-char cap truncates the result and the model mistakenly concludes this fetch was incomplete. For a specific window use `view_range=[<start>, <end>]`. The cache file preserves the article's block structure (one line per extracted chunk), so `view_range` lines map cleanly onto the article's logical paragraphs.

The cache directory `~/.dsh/cache/web-fetch/` is created on first use; old files accumulate and are not auto-cleaned.

## `web_search` (web-search-mmx)

Backed by the local `mmx` CLI (MiniMax OAuth). Returns up to 8 sources per call:

```ts
{
  sources: [{ url, title?, snippet?, publishedAt? }],
  truncated: boolean,
}
```

Override the mmx binary path with `DSH_WEB_SEARCH_MMX_BIN=<path>`.

## `web_fetch` (web-fetch-local)

Backed by a local Python `urllib` subprocess. Any public host is allowed; only loopback / private suffixes are blocked:

```
localhost / 127.0.0.1 / ::1 / 0.0.0.0    (loopback)
*.local / *.internal / *.lan              (private suffixes)
```

To add a custom blocklist rule, edit `PRIVATE_HOSTS` or `PRIVATE_SUFFIXES` in `web-fetch-local/src/fetch.py`. DSH HMR picks up the change on next `web_fetch` call.

The plugin defaults to `python3` on POSIX and `python` elsewhere; override with `DSH_WEB_FETCH_PYTHON=<path>`.

Cache files are written to `~/.dsh/cache/web-fetch/` and persist across sessions; the directory is created on first use. Filename is keyed by the URL hash:

```
web-fetch_<url-hash8>.txt
```

The same URL always maps to the same file, so repeated fetches overwrite in place.

## Verify

```bash
cat ~/.dsh/profiles/web/cordis.patch.yml
ls ~/.dsh/plugins/web-fetch-local ~/.dsh/plugins/web-search-mmx
ls ~/.dsh/.agent-presets/standard-custom
dsh web --dump-config | grep -E "web-search-mmx|web-fetch-local|tool-web"
```

## Layout

This clone (the install source):

```
my_dsh_plugins/
- README.md
- install.py
- presets/standard-custom/{agent.cordis.yml, preset.yml}
- web-search-mmx/{package.json, src/index.mjs}
- web-fetch-local/{package.json, src/{index.mjs, fetch.py}}
```

After install (DSH-managed):

```
~/.dsh/plugins/
- web-search-mmx/...
- web-fetch-local/...

~/.dsh/profiles/web/cordis.patch.yml
~/.dsh/.agent-presets/standard-custom/
~/.dsh/cache/web-fetch/                <- created on first web_fetch call
```

## Compatibility

- DSH >=0.1.0-rc.6, Node >=18.0.0
- install.py: Python ≥ 3.10 + PyYAML (`pip install pyyaml`)
- web-fetch-local: Python >=3.10
- web-search-mmx: `mmx` CLI on `PATH` (OAuth via `mmx auth login`)