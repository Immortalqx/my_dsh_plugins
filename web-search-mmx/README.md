# web-search-mmx

DSH plugin: `web_search` tool backed by local `mmx search query` (MiniMax OAuth). Drop-in replacement for the shipped DeepSeek-billed `web_search`.

## Install

CP install only. See the [top-level README](../README.md).

`~/.dsh/profiles/web/cordis.patch.yml` references this directory's `src/index.mjs` (rendered from [`cordis.patch.yml.template`](../cordis.patch.yml.template) at install time). After the standard CP install steps, `web_search` shows up at host scope. Selecting shipped `standard` preset would shadow it; pick `standard-custom` instead.

## Cross-platform mmx invocation

| OS | Invocation | Why |
|---|---|---|
| Windows | `cmd.exe /c mmx <args>` | Node 18+ doesn't consult PATHEXT, and CVE-2024-27980 blocks direct spawn of `.cmd`/`.bat`. Args are separate argv entries — no shell-string concatenation. |
| macOS / Linux | `mmx <args>` | mmx-cli installs as a shell wrapper. |

Override with `DSH_WEB_SEARCH_MMX_BIN=<path>`.

## Output shape

```ts
{
  sources: [{ url, title?, snippet?, publishedAt? }],
  truncated: boolean  // true if mmx returned more than 8 results
}
```

Result cap: 8 sources per call (matches shipped `searchMaxResults: 8`).

## Compatibility

- DSH `>=0.1.0-rc.6`
- Node `>=18.0.0`
- mmx-cli `>=1.0.16` (`mmx auth login` for OAuth)
- Windows 11 / macOS / Linux

## Limitations

- mmx-cli search-quality rules apply (multi-pass bilingual queries).
- mmx returns sources only — no generated answer prose.
- 8 source cap; refine the query for more.

## License

MIT — see [`LICENSE`](../LICENSE) in the repo root.