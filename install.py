#!/usr/bin/env python3
"""install.py — cross-platform installer for my_dsh_plugins.

Copies the plugin source dirs, the agent preset, and merges our patch rows
into `~/.dsh/profiles/web/cordis.patch.yml`. Designed to be:

  - Idempotent: re-running produces the same state.
  - Force-overwriting for files we own: the two plugin dirs and the preset
    are wiped before being re-copied (so any prior `cp -r src dst` nesting
    from a broken install is removed cleanly).
  - Additive for cordis.patch.yml: rows with ids other than the four we own
    are preserved verbatim — other plugins can share the patch file.

Platform detection is automatic; per-platform differences:
  - Windows   : `name:` is `file:///C:/.../index.mjs` (URL-encoded, forward slashes).
  - macOS / *nix: `name:` is a bare absolute POSIX path.

Usage:
    python install.py                  # actually install
    python install.py --dry-run        # print plan, write nothing
    python install.py --source <dir>   # override source repo root
    python install.py --target-home <dir>  # override $HOME (for testing)

Requires Python >= 3.10 (same as web-fetch-local/src/fetch.py).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Callable
from urllib.parse import quote

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "error: PyYAML is required. Install with:  pip install pyyaml\n"
        "  (or:  python -m pip install --user pyyaml)\n"
    )
    sys.exit(2)


# ---- constants -----------------------------------------------------------

DSH_HOME_SUBDIR = ".dsh"
PLUGINS_DIRNAME = "plugins"
PROFILES_DIRNAME = "profiles"
PROFILE_NAME = "web"
PATCH_FILENAME = "cordis.patch.yml"
PRESETS_DIRNAME = ".agent-presets"
PRESET_NAME = "standard-custom"

# Plugin source directories inside the repo (each ships `package.json` + `src/`).
PLUGIN_SOURCES = ("web-search-mmx", "web-fetch-local")

# cordis.patch.yml ids that we own: filter-then-rewrite these so the install is
# idempotent and doesn't clobber other plugins' rows.
#
#   web-search-mmx     — our plugin row (replaces shipped web_search)
#   web-fetch-local    — our plugin row (replaces shipped web_fetch)
#   web-search-deepseek— the shipped web_search we disable
#   tool-web           — the shipped web_fetch we disable
OWNED_PATCH_IDS = frozenset({
    "web-search-mmx",
    "web-fetch-local",
    "web-search-deepseek",
    "tool-web",
})

MIN_PYTHON = (3, 10)


# ---- small helpers -------------------------------------------------------

def log(msg: str) -> None:
    """Print a status line (always, even in --dry-run)."""
    print(msg)


def fatal(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    sys.stderr.write(f"error: {msg}\n")
    sys.exit(code)


def is_windows() -> bool:
    return sys.platform == "win32"


def format_plugin_name(abs_path: Path) -> str:
    """Render a plugin source file path as a cordis `name:` field.

    Windows   -> `file:///C:/Users/you/.dsh/plugins/web-search-mmx/src/index.mjs`
                 (forward slashes, URL-encoded special characters).
    POSIX     -> `/Users/you/.dsh/plugins/web-search-mmx/src/index.mjs`
                 (bare absolute path, no scheme).
    """
    posix = abs_path.as_posix()  # always forward slashes
    if is_windows():
        return f"file:///{quote(posix, safe='/')}"
    return posix


# ---- path resolution -----------------------------------------------------

def resolve_paths(source_root: Path, target_home: Path):
    """Compute every absolute path the installer needs."""
    plugins_dir = target_home / DSH_HOME_SUBDIR / PLUGINS_DIRNAME
    profile_dir = target_home / DSH_HOME_SUBDIR / PROFILES_DIRNAME / PROFILE_NAME
    patch_path = profile_dir / PATCH_FILENAME
    preset_dest = target_home / DSH_HOME_SUBDIR / PRESETS_DIRNAME / PRESET_NAME
    return plugins_dir, profile_dir, patch_path, preset_dest


# ---- plugin/preset dirs: force-overwrite copy ----------------------------

def install_dir(source: Path, dest: Path, dry_run: bool) -> None:
    """Wipe `dest` then copy `source` -> `dest`. Idempotent.

    Wiping first sidesteps the `cp -r src dst` nesting bug: even if a prior
    broken install left `dest/foo/` with a stale nested `dest/foo/foo/`, the
    rmtree below removes it cleanly before the fresh copy.
    """
    if not source.is_dir():
        fatal(f"source dir does not exist: {source}")
    if dest.exists():
        log(f"  rm    {dest}")
        if not dry_run:
            shutil.rmtree(dest)
    log(f"  cp -r {source} -> {dest}")
    if not dry_run:
        shutil.copytree(source, dest)


# ---- cordis.patch.yml: read / filter / write ------------------------------

def read_patch(patch_path: Path) -> list:
    """Return the parsed top-level YAML list from `patch_path`.

    Missing file -> []. Malformed YAML -> fatal (we never silently destroy
    a file the user owns; they have to fix it first, then re-run).
    """
    if not patch_path.exists():
        return []
    try:
        with patch_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        fatal(
            f"existing {patch_path} is not valid YAML: {e}\n"
            f"refusing to modify it — fix the file manually, then re-run install.py"
        )
    if data is None:
        return []
    if not isinstance(data, list):
        fatal(
            f"existing {patch_path} must be a YAML list of patch entries, "
            f"got {type(data).__name__}"
        )
    return data


def count_owned(entries: list) -> int:
    """How many entries the existing patch has that target one of our ids."""
    n = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") in OWNED_PATCH_IDS:
            n += 1
        ins = entry.get("insert")
        if isinstance(ins, list):
            for row in ins:
                if isinstance(row, dict) and row.get("id") in OWNED_PATCH_IDS:
                    n += 1
    return n


def filter_owned(entries: list) -> list:
    """Drop entries that target our owned ids; preserve everything else.

    - Top-level `{id: <owned>, disabled: true}` rows: removed.
    - Top-level `{insert: [...]}` blocks: nested rows with our ids are removed;
      the block itself is kept (with whatever other rows remain).
    - All other top-level rows: passed through unchanged.
    """
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            result.append(entry)
            continue
        if entry.get("id") in OWNED_PATCH_IDS:
            continue  # drop our owned disable row
        ins = entry.get("insert")
        if isinstance(ins, list):
            kept = [
                row for row in ins
                if not (isinstance(row, dict) and row.get("id") in OWNED_PATCH_IDS)
            ]
            if kept:
                entry = {**entry, "insert": kept}
                result.append(entry)
            # else: the block only contained our rows — drop the empty block
        else:
            result.append(entry)
    return result


def build_our_entries(plugins_dir: Path) -> list:
    """Build the patch entries we append on every install.

    Returns 3 top-level entries:
      1. disable shipped `web_search`
      2. disable shipped `web_fetch`
      3. insert our two plugins
    """
    def plugin_row(src_name: str) -> dict:
        name = format_plugin_name(
            (plugins_dir / src_name / "src" / "index.mjs").resolve()
        )
        return {"id": src_name, "name": name, "inject": ["tools"]}

    return [
        {"id": "web-search-deepseek", "disabled": True},
        {"id": "tool-web", "disabled": True},
        {"insert": [plugin_row("web-search-mmx"), plugin_row("web-fetch-local")]},
    ]


PATCH_HEADER = (
    "# cordis.patch.yml — managed by my_dsh_plugins/install.py.\n"
    "# Rows with id 'web-search-mmx', 'web-fetch-local',\n"
    "# 'web-search-deepseek', or 'tool-web' are owned by this installer:\n"
    "# re-running install.py replaces them with the latest versions.\n"
    "# Other rows (from other plugins or manual edits) are preserved.\n"
)


def write_patch(patch_path: Path, entries: list, dry_run: bool) -> None:
    """Write the merged patch list, creating parent dirs if needed."""
    log(f"  write {patch_path}  ({len(entries)} top-level entries)")
    if dry_run:
        return
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    with patch_path.open("w", encoding="utf-8") as f:
        f.write(PATCH_HEADER)
        yaml.safe_dump(
            entries,
            f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            width=4096,
        )


# ---- CLI -----------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="install.py",
        description="Cross-platform installer for my_dsh_plugins (DSH).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done, but write nothing.",
    )
    p.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Path to the repo root (default: this script's parent directory).",
    )
    p.add_argument(
        "--target-home",
        type=Path,
        default=None,
        help="Override $HOME (default: actual user home). Useful for testing.",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    if sys.version_info < MIN_PYTHON:
        fatal(
            f"Python >= {'.'.join(map(str, MIN_PYTHON))} required, "
            f"got {sys.version_info.major}.{sys.version_info.minor}"
        )

    args = parse_args(argv)
    here = Path(__file__).resolve().parent
    source_root = (args.source or here).resolve()
    target_home = (args.target_home or Path.home()).resolve()

    if not source_root.is_dir():
        fatal(f"--source is not a directory: {source_root}")

    plugins_dir, profile_dir, patch_path, preset_dest = resolve_paths(
        source_root, target_home
    )

    log(f"DSH home:    {target_home}")
    log(f"Source root: {source_root}")
    log(f"Platform:    {sys.platform}  ({'windows' if is_windows() else 'posix'})")
    log(f"Mode:        {'DRY-RUN' if args.dry_run else 'APPLY'}")
    log("")

    # 0. Validate the existing patch FIRST so a malformed file doesn't get
    #    partially-installed (plugin/preset dirs wiped, patch left untouched —
    #    would leave the system in a broken state we couldn't recover from
    #    without manual intervention).
    log(f"[0/3] validate existing patch -> {patch_path}")
    existing = read_patch(patch_path)
    owned_now = count_owned(existing)
    log(f"  read  {len(existing)} top-level entries "
        f"({owned_now} previously owned by this installer)")
    filtered = filter_owned(existing)
    ours = build_our_entries(plugins_dir)
    merged = filtered + ours
    log(f"  keep  {len(filtered)} entries from other plugins / manual edits")
    log(f"  append {len(ours)} entries owned by this installer")

    # 1. Plugin source dirs.
    log("")
    log("[1/3] plugin sources -> ~/.dsh/plugins/")
    for src_name in PLUGIN_SOURCES:
        install_dir(source_root / src_name, plugins_dir / src_name, args.dry_run)

    # 2. Agent preset (mandatory: disables shipped web_search/web_fetch so our
    #    name-collision overrides actually take effect).
    log("")
    log("[2/3] agent preset -> ~/.dsh/.agent-presets/standard-custom/")
    install_dir(source_root / "presets" / PRESET_NAME, preset_dest, args.dry_run)

    # 3. cordis.patch.yml: write the validated merged list.
    log("")
    log(f"[3/3] merge patch rows -> {patch_path}")
    write_patch(patch_path, merged, args.dry_run)

    log("")
    log("Done.")
    log("In the browser: F5, then Settings -> Agent preset -> custom -> pick")
    log("'Standard (custom)'. cordis.patch.yml is hot-reloaded automatically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
