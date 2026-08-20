#!/usr/bin/env python3
"""install.py — cross-platform installer for my_dsh_plugins.

Copies the plugin source dirs and the agent preset into the DSH home
(`~/.dsh/`). The preset ships with two ``__PLUGIN_PATH_*__`` placeholders
that install.py substitutes with absolute paths at install time.

Designed to be:

  - Idempotent: re-running produces the same state.
  - Force-overwriting for files we own: the two plugin dirs and the preset
    are wiped before being re-copied (so any prior `cp -r src dst` nesting
    from a broken install is removed cleanly).

Usage:
    python install.py                  # actually install
    python install.py --dry-run        # print plan, write nothing
    python install.py --source <dir>   # override source repo root
    python install.py --target-home <dir>  # override $HOME (for testing)

Requires Python >= 3.10.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from urllib.parse import quote


# ---- constants -----------------------------------------------------------

DSH_HOME_SUBDIR = ".dsh"
PLUGINS_DIRNAME = "plugins"
PRESETS_DIRNAME = ".agent-presets"
PRESET_NAME = "standard-mmx"

# Plugin source directories inside the repo (each ships `package.json` + `src/`).
PLUGIN_SOURCES = ("web-search-mmx", "web-fetch-local")

# Placeholders in presets/standard-mmx/agent.cordis.yml that install.py
# substitutes with absolute plugin paths.
PLUGIN_PATH_PLACEHOLDERS = {
    "__PLUGIN_PATH_WEB_SEARCH_MMX__": "web-search-mmx",
    "__PLUGIN_PATH_WEB_FETCH_LOCAL__": "web-fetch-local",
}

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
    """Render an absolute plugin index path as a cordis `name:` field.

    Windows   -> `file:///C%3A/Users/you/.dsh/plugins/.../index.mjs`
                 (forward slashes, URL-encoded `:`).
    POSIX     -> `/Users/you/.dsh/plugins/.../index.mjs`
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
    preset_dest = target_home / DSH_HOME_SUBDIR / PRESETS_DIRNAME / PRESET_NAME
    return plugins_dir, preset_dest


# ---- plugin dirs: force-overwrite copy ------------------------------------

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


# ---- preset: copy with placeholder substitution ---------------------------

def render_preset(source_yml: Path, plugins_dir: Path, dry_run: bool) -> str:
    """Return the agent.cordis.yml text with __PLUGIN_PATH_*__ substituted.

    Each placeholder is replaced with the absolute path of
    `plugins_dir/<plugin>/src/index.mjs`. Missing placeholders are reported
    but not fatal.
    """
    text = source_yml.read_text(encoding="utf-8")
    missing = []
    for placeholder, plugin_id in PLUGIN_PATH_PLACEHOLDERS.items():
        abs_path = (plugins_dir / plugin_id / "src" / "index.mjs").resolve()
        replacement = format_plugin_name(abs_path)
        if placeholder not in text:
            missing.append(placeholder)
            continue
        text = text.replace(placeholder, replacement)
    if missing:
        log(
            f"  note  placeholders not found in {source_yml.name} "
            f"(template may have been edited): {', '.join(missing)}"
        )
    return text


def install_preset(source_root: Path, preset_dest: Path, plugins_dir: Path,
                   dry_run: bool) -> None:
    """Copy the preset dir, substituting plugin paths into agent.cordis.yml.

    preset.yml is static and copied verbatim. agent.cordis.yml is read,
    has its ``__PLUGIN_PATH_*__`` placeholders replaced with absolute
    plugin paths, and written back. Other files (if any) are copied as-is.
    """
    source_dir = source_root / "presets" / PRESET_NAME
    if not source_dir.is_dir():
        fatal(f"preset source dir does not exist: {source_dir}")
    if preset_dest.exists():
        log(f"  rm    {preset_dest}")
        if not dry_run:
            shutil.rmtree(preset_dest)
    log(f"  cp -r {source_dir} -> {preset_dest}")
    if dry_run:
        return
    preset_dest.mkdir(parents=True, exist_ok=True)
    for entry in source_dir.iterdir():
        if entry.is_dir():
            shutil.copytree(entry, preset_dest / entry.name)
            continue
        if entry.name == "agent.cordis.yml":
            rendered = render_preset(entry, plugins_dir, dry_run=False)
            (preset_dest / entry.name).write_text(rendered, encoding="utf-8")
        else:
            shutil.copy2(entry, preset_dest / entry.name)


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

    plugins_dir, preset_dest = resolve_paths(source_root, target_home)

    log(f"DSH home:    {target_home}")
    log(f"Source root: {source_root}")
    log(f"Platform:    {sys.platform}  ({'windows' if is_windows() else 'posix'})")
    log(f"Mode:        {'DRY-RUN' if args.dry_run else 'APPLY'}")
    log("")

    # 1. Plugin source dirs.
    log("[1/2] plugin sources -> ~/.dsh/plugins/")
    for src_name in PLUGIN_SOURCES:
        install_dir(source_root / src_name, plugins_dir / src_name, args.dry_run)

    # 2. Agent preset.
    log("")
    log("[2/2] agent preset -> ~/.dsh/.agent-presets/standard-mmx/")
    install_preset(source_root, preset_dest, plugins_dir, args.dry_run)

    log("")
    log("Done.")
    log("In the browser: F5, then Settings -> Agent preset -> custom -> pick")
    log("'Standard (mmx)'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
