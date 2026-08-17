# my_dsh_plugins

`web_search` (mmx-cli) + `web_fetch` (Python urllib) plugins for DSH.

## Install

Drop the two files into DSH's global directories. The plugin code stays in this clone; DSH loads it from there.

```bash
REPO=/path/to/my_dsh_plugins

sed "s|__REPO_PATH__|$REPO|g" "$REPO/cordis.patch.yml.template" \
    > ~/.dsh/profiles/web/cordis.patch.yml

cp -r "$REPO/presets/standard-custom" ~/.dsh/.agent-presets/standard-custom

# Browser: F5, then Settings -> Agent preset -> custom -> pick "Standard (custom)"
```

Windows: run in git-bash or WSL.

## Verify

```bash
cat ~/.dsh/profiles/web/cordis.patch.yml
ls ~/.dsh/.agent-presets/standard-custom
dsh web --dump-config | grep -E "web-search-mmx|web-fetch-local|tool-web"
python "$REPO/web-fetch-local/src/fetch.py" --url https://github.com
```

## Layout

```
my_dsh_plugins/
- README.md
- cordis.patch.yml.template
- presets/standard-custom/{agent.cordis.yml, preset.yml}
- web-search-mmx/{README.md, package.json, src/index.mjs}
- web-fetch-local/{README.md, package.json, src/{index.mjs, fetch.py}}
```

## Compatibility

- DSH >=0.1.0-rc.6, Node >=18.0.0
- web-fetch-local: Python >=3.10
- web-search-mmx: mmx CLI on PATH (OAuth via `mmx auth login`)
- Linux / macOS / Windows (via git-bash / WSL)

## License

MIT.