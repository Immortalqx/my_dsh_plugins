# my_dsh_plugins

`web_search` (mmx-cli) + `web_fetch` (Python urllib) plugins for DSH, scoped to the `standard-mmx` agent preset only.

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

Requirements: Python ≥ 3.10 (no other deps).

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

## `web_fetch` (web-fetch-local)

Backed by a local Python `urllib` subprocess. Any public host is allowed; only loopback / private suffixes are blocked:

```
localhost / 127.0.0.1 / ::1 / 0.0.0.0 / 0    (loopback)
*.local / *.internal / *.lan                  (private suffixes)
```

To add a custom blocklist rule, edit `PRIVATE_HOSTS` or `PRIVATE_SUFFIXES` in `web-fetch-local/src/fetch.py`. DSH HMR picks up the change on next `web_fetch` call.

The plugin defaults to `python3` on POSIX and `python` elsewhere; override with `DSH_WEB_FETCH_PYTHON=<path>`.

`web_fetch` returns a short inline preview (first ~2000 chars) plus the absolute path to a cache file that holds the full extracted text. The inline preview is truncated — read the cache file with `str_replace_editor` to see the complete page.

Cache files are written to `~/.dsh/cache/web-fetch/` and persist across sessions; the directory is created on first use. Filename is keyed by the URL hash:

```
web-fetch_<url-hash8>.txt
```

The same URL always maps to the same file, so repeated fetches overwrite in place.

## Verify

```bash
ls ~/.dsh/plugins/web-fetch-local ~/.dsh/plugins/web-search-mmx
ls ~/.dsh/.agent-presets/standard-mmx
```

## Layout

This clone (the install source):

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
- web-fetch-local/...
~/.dsh/.agent-presets/standard-mmx/
~/.dsh/cache/web-fetch/                <- created on first web_fetch call
```

## Compatibility

- DSH >=0.1.0-rc.6, Node >=18.0.0
- install.py: Python ≥ 3.10
- web-fetch-local: Python >=3.10
- web-search-mmx: `mmx` CLI on `PATH` (OAuth via `mmx auth login`)
