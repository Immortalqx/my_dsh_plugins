#!/usr/bin/env python3
"""rebase_preset.py — regenerate `presets/standard-mmx/agent.cordis.yml`
from the `standard` preset shipped inside the installed DSH.

Why this exists
---------------
`standard-mmx` is deliberately "the shipped `standard` agent composition plus
two local web tools". The shipped composition is the authoritative list of
rows for a given DSH release: it changes between releases (rows added, config
keys renamed, package names replaced), and every one of those changes breaks a
hand-maintained copy. Instead of diffing by hand on each upgrade, this script
re-derives our file from the installed shipped file and re-applies our three
deltas:

  1. the leading comment block (rewritten to describe this preset),
  2. `disabled: true` on the shipped `tool-web` row (its `web_search` /
     `web_fetch` are what our two plugins replace),
  3. two appended rows registering the local plugins, carrying the
     `__PLUGIN_PATH_*__` placeholders that `install.py` substitutes.

Everything else — row order, config values, comments, the `!!js` gates — is
copied verbatim, so the result is exactly as loadable as its source.

The script fails loudly when the shipped composition no longer has the shape
it edits (no `tool-web` row, no leading comment block), because a silent
no-op here produces a preset that looks fine and mounts wrong.

Usage:
    python tools/rebase_preset.py                 # write presets/standard-mmx/agent.cordis.yml
    python tools/rebase_preset.py --dry-run       # print the result, write nothing
    python tools/rebase_preset.py --dsh <dir>     # explicit DSH package dir
    python tools/rebase_preset.py --check         # exit 1 if the file is out of date

Requires Python >= 3.10.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = REPO_ROOT / "presets" / "standard-mmx" / "agent.cordis.yml"

SHIPPED_PRESET_REL = Path(
    "node_modules/@deepseek-ai/dsh-agent-presets/presets/standard/agent.cordis.yml"
)

# The row whose shipped web tools we replace. `web_search` and `web_fetch`
# both come from this one package in DSH >= 0.1.5.
DISABLED_ROW_ID = "tool-web"
DISABLED_ROW_NAME = "'@deepseek-ai/dsh-tool-web'"

# Appended rows. The placeholders are substituted by install.py with the
# absolute `file:///...` URL of each plugin's `src/index.mjs`.
LOCAL_ROWS = """
# ── local web tools (standard-mmx delta) ────────────────────────────────────
#
# `web_search` (mmx-cli / MiniMax OAuth) and `web_fetch` (local Python urllib
# + Node Turndown) replace the shipped pair disabled above. Both rows only
# register into the host `tools` registry and publish no service, so neither
# needs a group realm. install.py substitutes the two placeholder names below
# with each plugin's absolute `index.mjs` path at install time.

- id: web-search-mmx
  name: __PLUGIN_PATH_WEB_SEARCH_MMX__
  inject: ['tools']

- id: web-fetch-local
  name: __PLUGIN_PATH_WEB_FETCH_LOCAL__
  inject: ['tools']
"""


def log(msg: str) -> None:
    print(msg)


def fatal(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    sys.stderr.write(f"error: {msg}\n")
    sys.exit(1)


# ---- locating the installed DSH -------------------------------------------

def find_dsh_root(explicit: Path | None) -> Path:
    """Return the directory of the installed `@deepseek-ai/dsh` package.

    Order: `--dsh`, then the global npm roots for this platform. A candidate
    only counts when it carries both the package manifest and the shipped
    preset we are about to read.
    """
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    if sys.platform == "win32":
        candidates.append(Path.home() / "AppData" / "Roaming" / "npm" / "node_modules")
        candidates.append(Path(r"C:\Program Files\nodejs\node_modules"))
    else:
        candidates.append(Path("/usr/local/lib/node_modules"))
        candidates.append(Path("/opt/homebrew/lib/node_modules"))
        candidates.append(Path.home() / ".npm-global" / "lib" / "node_modules")

    for root in candidates:
        pkg = root if root.name == "dsh" else root / "@deepseek-ai" / "dsh"
        if (pkg / "package.json").is_file() and (pkg / SHIPPED_PRESET_REL).is_file():
            return pkg
    tried = ", ".join(str(c) for c in candidates)
    fatal(
        "could not locate the installed @deepseek-ai/dsh package "
        f"(no package.json + shipped standard preset under: {tried}). "
        "Pass --dsh <path-to>/node_modules/@deepseek-ai/dsh"
    )


def read_dsh_version(dsh_root: Path) -> str:
    try:
        return json.loads((dsh_root / "package.json").read_text(encoding="utf-8"))["version"]
    except Exception as exc:  # pragma: no cover - malformed install
        fatal(f"cannot read version from {dsh_root / 'package.json'}: {exc}")


# ---- rendering ------------------------------------------------------------

def header(dsh_version: str) -> str:
    """The comment block that replaces the shipped file's own header."""
    return f"""# `standard-mmx`: the shipped `standard` agent preset plus local web tools.
#
# GENERATED FILE — do not hand-edit rows here.
# Re-derive it with `python tools/rebase_preset.py` after a DSH upgrade; see
# that script for the exact deltas it re-applies. Copied verbatim from the
# `standard` preset shipped in @deepseek-ai/dsh {dsh_version}, so every row,
# config value, `!!js` gate, and comment below is the upstream one. The only
# deltas are at the bottom of the file:
#
#   1. `tool-web` is disabled — its `web_search` / `web_fetch` are the two
#      tools this preset replaces;
#   2. `web-search-mmx` and `web-fetch-local` register the local replacements.
#
# Because the shipped `standard` composition changes between DSH releases, a
# stale copy of it is the first thing to break on an upgrade. `--check` mode
# reports whether this file still matches the installed DSH.
#
# The shipped header this replaces explains the plane split (host composition
# keeps the registries, sandbox, persistence, and model route; a preset
# contributes what one session adds to them) and the realm rule (a row here
# that publishes a service must sit in a group carrying an `isolate` realm).
# Both rules still apply to every row below.
"""


def rebase(shipped_text: str, dsh_version: str) -> str:
    lines = shipped_text.splitlines(keepends=True)

    # (1) Replace the leading comment block: everything before the first row.
    first_row = next(
        (i for i, line in enumerate(lines) if line.startswith("- id:")),
        None,
    )
    if first_row is None:
        fatal("shipped composition has no top-level `- id:` row; refusing to guess")
    body = lines[first_row:]

    # (2) Disable the shipped web-tool row.
    row_start = next(
        (i for i, line in enumerate(body) if line.rstrip("\n") == f"- id: {DISABLED_ROW_ID}"),
        None,
    )
    if row_start is None:
        fatal(
            f"shipped composition has no `- id: {DISABLED_ROW_ID}` row. The web tools "
            "may have moved to another package: re-read the shipped preset and "
            "update this script's delta list."
        )
    name_line = next(
        (
            i
            for i in range(row_start, min(row_start + 6, len(body)))
            if body[i].strip() == f"name: {DISABLED_ROW_NAME}"
        ),
        None,
    )
    if name_line is None:
        fatal(
            f"`- id: {DISABLED_ROW_ID}` is not followed by `name: {DISABLED_ROW_NAME}`; "
            "the row changed shape — re-read the shipped preset."
        )
    if any(body[i].strip() == "disabled: true" for i in range(name_line, min(name_line + 4, len(body)))):
        fatal("the shipped row is already disabled; is this preset the shipped file itself?")
    body.insert(name_line + 1, "  disabled: true\n")

    # (3) Append the local plugin rows.
    rendered = header(dsh_version) + "\n" + "".join(body)
    if not rendered.endswith("\n"):
        rendered += "\n"
    return rendered + LOCAL_ROWS


# ---- CLI ------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="rebase_preset.py",
        description="Regenerate presets/standard-mmx/agent.cordis.yml from the "
                    "installed shipped `standard` preset.",
    )
    p.add_argument("--dsh", type=Path, default=None,
                   help="Path to the installed @deepseek-ai/dsh package directory.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the rendered composition, write nothing.")
    p.add_argument("--check", action="store_true",
                   help="Exit 1 when the checked-in file differs from a fresh render.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    if sys.version_info < (3, 10):
        fatal("Python >= 3.10 required")
    args = parse_args(argv)

    dsh_root = find_dsh_root(args.dsh)
    dsh_version = read_dsh_version(dsh_root)
    shipped_file = dsh_root / SHIPPED_PRESET_REL
    rendered = rebase(shipped_file.read_text(encoding="utf-8"), dsh_version)

    if args.check:
        current = OUTPUT_FILE.read_text(encoding="utf-8") if OUTPUT_FILE.is_file() else ""
        if current == rendered:
            log(f"up to date with @deepseek-ai/dsh {dsh_version}")
            return 0
        log(f"OUT OF DATE relative to @deepseek-ai/dsh {dsh_version}: "
            f"{OUTPUT_FILE.relative_to(REPO_ROOT)}")
        log("re-run without --check to regenerate it, then review the diff.")
        return 1

    if args.dry_run:
        sys.stdout.write(rendered)
        return 0

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    previous = OUTPUT_FILE.read_text(encoding="utf-8") if OUTPUT_FILE.is_file() else None
    OUTPUT_FILE.write_text(rendered, encoding="utf-8")
    log(f"wrote {OUTPUT_FILE.relative_to(REPO_ROOT)} from {shipped_file}")
    log(f"DSH version: {dsh_version}")
    if previous is None:
        log("(new file)")
    elif previous == rendered:
        log("(unchanged)")
    else:
        log("(changed — review with `git diff`)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
