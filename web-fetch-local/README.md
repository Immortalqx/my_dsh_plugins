# web-fetch-local

DSH plugin: `web_fetch` tool backed by local Python `urllib` + an SSRF blocklist. Drop-in replacement for the shipped (default-disabled) `web_fetch`.

## Install

CP install only. See the [top-level README](../README.md).

`~/.dsh/profiles/web/cordis.patch.yml` references this directory's `src/index.mjs` (rendered from [`cordis.patch.yml.template`](../cordis.patch.yml.template) at install time). Selecting shipped `standard` preset would shadow it; pick `standard-custom` instead.

## URL blocking

There is **no public-host allowlist** — anything reachable on the public internet is allowed. `web_search` (mmx-cli, MiniMax OAuth) already filters search results; over-blocking on `web_fetch` would just lock the agent out of legitimate pages.

Only loopback / private-host targets are blocked by default in `src/fetch.py`:

```
localhost / 127.0.0.1 / ::1 / 0.0.0.0   (loopback)
*.local / *.internal / *.lan             (private suffixes)
```

To add a custom blocklist rule, edit `PRIVATE_HOSTS` or `PRIVATE_SUFFIXES` in `src/fetch.py`; DSH HMR picks up the change on next `web_fetch` call.

## Cross-platform Python

| OS | Default | Override |
|---|---|---|
| Windows | `python` | `DSH_WEB_FETCH_PYTHON=py` |
| macOS / Linux | `python3` | `DSH_WEB_FETCH_PYTHON=python3.12` |

## Pagination

The `url` parameter is required on every paginated call. The `render()` output, when `truncated: true`, explicitly writes the next-call invocation with the URL already filled in, so the model can copy it directly:

```
NEXT CALL: web_fetch(url="...", offset_bytes=<N>)
```

Step-by-step:

1. `web_fetch(url=U)` → first chunk; response carries `truncated`, `bytes`, `offsetBytes`, `totalBytes`
2. If `truncated: true`: `web_fetch(url=U, offset_bytes=N)` — must re-pass `url`
3. Repeat until `truncated: false`

**Why this works on servers that ignore `Range`**: some servers (Wikipedia, a few CDNs) return HTTP 200 with the full body when they see a `Range` header. The Python helper detects this (`status != 206`), reads up to 10MB locally, and slices in-process.

## Output shape

```ts
{
  url, statusCode, body, truncated,
  offsetBytes, totalBytes,   // 0 = server didn't disclose length
  bytes
}
```

## Compatibility

- DSH `>=0.1.0-rc.6`
- Python `>=3.10`
- Windows 11 / macOS / Linux

## Limitations

- No host allowlist — any public host works, including Yahoo Finance, xueqiu, webull, eastmoney, baike.baidu, etc.
- HTML→text is a tag strip (no Turndown, no markdown conversion).
- No JavaScript rendering.

## License

MIT — see [`LICENSE`](../LICENSE) in the repo root.
