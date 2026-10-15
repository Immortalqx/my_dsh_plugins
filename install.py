#!/usr/bin/env python3
"""install.py — cross-platform installer for my_dsh_plugins.

Copies the plugin source dirs into the DSH home (`~/.dsh/`) and renders the
`standard-mmx` agent-preset Bundle next to them. The Bundle ships one
`@deepseek-ai/dsh-agent-preset` declaration row whose `config.plugins` list is
the shipped `standard` composition with three local deltas; the two plugin rows
inside it carry `__PLUGIN_PATH_*__` placeholders that install.py substitutes
with absolute paths at install time, so nothing has to be edited by hand after
a DSH upgrade.

For each plugin, install.py also:

  - Creates a directory junction at `~/.dsh/plugins/<name>/node_modules`
    pointing into DSH's bundled `node_modules/`. Node module resolution walks
    up from `src/index.mjs` looking for a `node_modules/` directory; without
    the junction it cannot see DSH's `@deepseek-ai/dsh-tools` (used by every
    plugin that calls `defineTool()`) or, for `web-fetch-local`, DSH's
    `turndown` and `@joplin/turndown-plugin-gfm`. The junction uses
    `mklink /J` on Windows (no admin required) and `os.symlink` on POSIX.
  - Recommends the optional Python deps for `web-fetch-local` (structured-data
    extraction: JSON-LD, OpenGraph, Microdata, RDFa, Microformat). Without
    `extruct`, `metadata` in the tool result is `null` and `metadataKind` is
    `"none"`; without `brotli`, `br`-encoded responses cannot be decoded.

Finally it runs `tools/verify-contract.mjs` against the installed plugins
(`--no-verify` to skip): that probe re-checks the model-facing tool schemas
against the *installed* `@deepseek-ai/dsh-tools` compiler, which is the part
of a DSH upgrade most likely to break a plugin silently. See
`tools/rebase_preset.py` for the other half — regenerating the preset Bundle
from the shipped `standard` composition after a DSH upgrade.

One step install.py cannot do for you
--------------------------------------
Since DSH 0.2.0 an agent preset is a Loader row, and Loader rows reach a
profile through a Bundle — which only `plugin_manager install_bundle` may
install. install.py therefore renders the Bundle and prints the exact call;
run it once (or install from the Plugins page in the sidebar). Everything else
— plugin sources, junctions, cache dir, contract probe — this script owns.

Designed to be:

  - Idempotent: re-running produces the same state.
  - Force-overwriting for files we own: the two plugin dirs and the rendered
    Bundle are wiped before being re-written (so any prior `cp -r src dst`
    nesting from a broken install is removed cleanly).
  - Validating before writing: the Bundle patch is rendered and checked for
    unsubstituted placeholders BEFORE anything is copied to disk, so a broken
    template cannot leave a half-installed state.

Usage:
    python install.py                  # actually install
    python install.py --dry-run        # print plan, write nothing
    python install.py --source <dir>   # override source repo root
    python install.py --target-home <dir>  # override $HOME (for testing)
    python install.py --no-verify      # skip the post-install contract probe

Requires Python >= 3.10.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote


# ---- constants -----------------------------------------------------------

DSH_HOME_SUBDIR = ".dsh"
PLUGINS_DIRNAME = "plugins"
BUNDLES_DIRNAME = "bundles"
BUNDLE_NAME = "standard-mmx"

# The agent preset directory that DSH 0.1.x discovered by scanning
# `~/.dsh/.agent-presets/`. Since 0.2.0 nothing scans it; install.py removes
# our own stale copy (and only ours) so the dead preset cannot be mistaken for
# a working one.
LEGACY_PRESETS_DIRNAME = ".agent-presets"
LEGACY_PRESET_NAME = "standard-mmx"

# Plugin source directories inside the repo (each ships `package.json` + `src/`).
PLUGIN_SOURCES = ("web-search-mmx", "web-fetch-local")

# Bundle source directory inside the repo: `package.json` + generated
# `cordis.patch.yml`.
BUNDLE_SOURCE_DIRNAME = "bundle"

# Placeholders in bundle/cordis.patch.yml that install.py substitutes with
# absolute plugin paths.
PLUGIN_PATH_PLACEHOLDERS = {
    "__PLUGIN_PATH_WEB_SEARCH_MMX__": "web-search-mmx",
    "__PLUGIN_PATH_WEB_FETCH_LOCAL__": "web-fetch-local",
}

# Plugin-specific optional dependencies (Python). Soft-imported at runtime;
# install.py recommends them but does NOT fail if `pip install` cannot run.
PLUGIN_PYTHON_DEPS = {
    "web-fetch-local": ["extruct", "brotli"],
}

# Plugin-specific Node modules to bridge from DSH's bundled node_modules into
# the plugin's own node_modules/ via a junction. Keys are plugin names; values
# are lists of npm package directory names (must exist under
# `<DSH root>/node_modules/@deepseek-ai/dsh/node_modules/<name>`).
#
# `@deepseek-ai/dsh-tools` is the model-facing tool compiler; both register
# `web_search`/`web_fetch` through `defineTool()` so they need it resolvable
# from the plugin's `src/*.mjs` at module-load time. Without this bridge, the
# `import { defineTool } from '@deepseek-ai/dsh-tools'` line throws
# `ERR_MODULE_NOT_FOUND` and the plugin fails to load.
PLUGIN_NODE_BRIDGE = {
    "web-search-mmx": ["@deepseek-ai/dsh-tools"],
    "web-fetch-local": ["@deepseek-ai/dsh-tools", "turndown", "@joplin/turndown-plugin-gfm"],
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
    return sys.platform == 'win32'


def format_plugin_name(abs_path: Path) -> str:
    """Render an absolute plugin index path as a cordis `name:` field.

    Windows   -> `file:///C%3A/Users/you/.dsh/plugins/.../index.mjs`
                 (forward slashes, URL-encoded `:`).
    POSIX     -> `/Users/you/.dsh/plugins/.../index.mjs`
                 (bare absolute path, no scheme).

    DSH's Loader hands a non-`cordis:`, non-`.` specifier straight to Node's
    `import()`, and Node ESM resolves an absolute `file:` URL natively
    (percent-decoding it) as well as a POSIX absolute path. Verified unchanged
    in 0.2.0-rc.2.
    """
    posix = abs_path.as_posix()  # always forward slashes
    if is_windows():
        return f"file:///{quote(posix, safe='/')}"
    return posix


# ---- path resolution -----------------------------------------------------

def resolve_paths(source_root: Path, target_home: Path):
    """Compute every absolute path the installer needs."""
    dsh_home = target_home / DSH_HOME_SUBDIR
    plugins_dir = dsh_home / PLUGINS_DIRNAME
    bundle_dest = dsh_home / BUNDLES_DIRNAME / BUNDLE_NAME
    legacy_preset = dsh_home / LEGACY_PRESETS_DIRNAME / LEGACY_PRESET_NAME
    # DSH's installed package, whose bundled node_modules/ holds its npm deps
    # (including turndown and @deepseek-ai/dsh-tools).
    dsh_package = _find_dsh_package()
    dsh_node_modules = (dsh_package / "node_modules") if dsh_package else None
    return plugins_dir, bundle_dest, legacy_preset, dsh_node_modules, dsh_package


def _find_dsh_package() -> Path | None:
    """Locate the installed `@deepseek-ai/dsh` package directory.

    On a typical npm install this is
    `~/AppData/Roaming/npm/node_modules/@deepseek-ai/dsh` on Windows (where
    `npm install -g @deepseek-ai/dsh` lands) or
    `/usr/local/lib/node_modules/@deepseek-ai/dsh` on POSIX. We probe a few
    known locations and return the first one that carries both a package
    manifest and a bundled `node_modules/` directory.

    Returns None when nothing matches — install.py then skips the junction
    step and the plugin degrades gracefully to text-only output.
    """
    candidates = []
    if is_windows():
        candidates.append(Path.home() / "AppData" / "Roaming" / "npm" / "node_modules")
        candidates.append(Path(r"C:\Program Files\nodejs\node_modules"))
    else:
        candidates.append(Path("/usr/local/lib/node_modules"))
        candidates.append(Path.home() / ".npm-global" / "lib" / "node_modules")
        candidates.append(Path("/opt/homebrew/lib/node_modules"))
    for cand in candidates:
        dsh_pkg = cand / "@deepseek-ai" / "dsh"
        if (dsh_pkg / "package.json").is_file() and (dsh_pkg / "node_modules").is_dir():
            return dsh_pkg
    return None


def read_dsh_version(dsh_package: Path | None) -> str:
    """The installed DSH version, or a placeholder when it cannot be read.

    Printed at install time so the log records which release the plugins were
    installed against — the fact that matters when a tool later misbehaves.
    """
    if dsh_package is None:
        return "(not found)"
    try:
        import json
        return json.loads((dsh_package / "package.json").read_text(encoding="utf-8"))["version"]
    except Exception:
        return "(unreadable)"


# ---- plugin dirs: force-overwrite copy ------------------------------------

def install_dir(source: Path, dest: Path, dry_run: bool) -> None:
    """Wipe `dest` then copy `source` -> `dest`. Idempotent.

    Wiping first sidesteps the `cp -r src dst` nesting bug: even if a prior
    broken install left `dest/foo/` with a stale nested `dest/foo/foo/`, the
    rmtree below removes it cleanly before the fresh copy.

    `node_modules` and `__pycache__` are never copied: the junction (and any
    stray Python bytecode) is per-installation state that `copytree` would
    otherwise follow into DSH's own node_modules — a multi-hundred-megabyte
    recursive copy.
    """
    if not source.is_dir():
        fatal(f"source dir does not exist: {source}")
    ignore = shutil.ignore_patterns("node_modules", "__pycache__", "*.pyc")
    if dest.exists():
        log(f"  rm    {dest}")
        if not dry_run:
            shutil.rmtree(dest)
    log(f"  cp -r {source} -> {dest}")
    if not dry_run:
        shutil.copytree(source, dest, ignore=ignore)


# ---- plugin-specific: node_modules junction + python deps ---------------

def bridge_plugin_node_modules(plugin_dest: Path, dsh_node_modules: Path,
                                packages: list[str], dry_run: bool) -> None:
    """Create `plugin_dest/node_modules/` as a junction into DSH's
    node_modules/, so Node module resolution from `plugin_dest/src/*.mjs`
    can find packages shipped under `dsh_node_modules/<name>`.

    Behavior on missing packages: skip silently (the corresponding import
    fails later, and the tool degrades to text-only mode). We don't fail
    the install — bridge creation is best-effort.
    """
    if dsh_node_modules is None or not dsh_node_modules.is_dir():
        log(f"  note  DSH's node_modules not found; skipping junction for {plugin_dest.name}")
        return

    target_node_modules = plugin_dest / "node_modules"
    if target_node_modules.exists() or target_node_modules.is_symlink():
        log(f"  rm    {target_node_modules}")
        if not dry_run:
            # Junction on Windows, symlink elsewhere. Both are removed with
            # shutil.rmtree which follows junctions/symlinks; for symlinks
            # pointing to a directory use `unlink` instead.
            if target_node_modules.is_symlink() and not target_node_modules.is_dir():
                target_node_modules.unlink()
            else:
                shutil.rmtree(target_node_modules, ignore_errors=True)

    log(f"  ln -s {dsh_node_modules} -> {target_node_modules}  (junction)")
    if dry_run:
        return
    if is_windows():
        # `mklink /J` creates a directory junction (no admin required on
        # modern Windows). Python's os.symlink needs admin for directory
        # symlinks on Windows; junctions sidestep that.
        try:
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(target_node_modules), str(dsh_node_modules)],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as e:
            log(f"  note  mklink /J failed: {e.stderr.strip() or e}")
            log(f"        the plugin will degrade to text-only output")
    else:
        try:
            target_node_modules.symlink_to(dsh_node_modules, target_is_directory=True)
        except OSError as e:
            log(f"  note  symlink failed: {e}")
            log(f"        the plugin will degrade to text-only output")

    # Verify each requested package is reachable through the junction.
    for pkg in packages:
        target_pkg = target_node_modules / pkg
        if target_pkg.is_dir():
            log(f"  [+]   {pkg} reachable via junction")
        else:
            log(f"  note  {pkg} not found under DSH's node_modules; skipping")


def recommend_python_deps(plugin_name: str, deps: list[str], dry_run: bool) -> None:
    """Print a `pip install` recommendation for the plugin's optional
    dependencies. install.py does NOT install them automatically — they're
    soft-imported at runtime, and the user can choose to add them later.
    """
    if not deps:
        return
    log(f"  note  {plugin_name} recommends Python deps: {', '.join(deps)}")
    log(f"        into the per-user venv the plugin auto-discovers:")
    log(f"          python -m venv ~/.dsh/venv")
    venv_py = "~/.dsh/venv/Scripts/python.exe" if is_windows() else "~/.dsh/venv/bin/python"
    log(f"          {venv_py} -m pip install {' '.join(deps)}")
    log(f"        without them the tool still works, with degraded metadata/encoding")


# ---- bundle: render the patch with plugin paths substituted ---------------

def render_bundle_patch(source_yml: Path, plugins_dir: Path, dry_run: bool) -> str:
    """Return the Bundle patch text with __PLUGIN_PATH_*__ substituted.

    Each placeholder is replaced with the absolute path of
    `plugins_dir/<plugin>/src/index.mjs`. A placeholder the template no longer
    carries is reported as a note; a placeholder that survives substitution is
    fatal, because the row's `name:` would then not resolve and the registry
    would report the whole preset broken at declaration time.

    The guard matches the placeholder LEXEME (`__PLUGIN_PATH_[A-Z0-9_]*__`),
    not the bare prefix — a prefix match also hits the explanatory prose in the
    comments, which made the original guard fatal on a correct file.
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
    leftover = re.search(r"__PLUGIN_PATH_[A-Z0-9_]*__", text)
    if leftover is not None:
        line = next(
            (l.strip() for l in text.splitlines() if leftover.group(0) in l), ""
        )
        fatal(
            f"{source_yml} still carries an unsubstituted {leftover.group(0)} "
            f"placeholder after rendering ({line!r}). Add it to "
            "PLUGIN_PATH_PLACEHOLDERS in install.py, or regenerate the file "
            "with `python tools/rebase_preset.py`."
        )
    return text


def install_bundle(source_root: Path, bundle_dest: Path, plugins_dir: Path,
                   dry_run: bool) -> None:
    """Render the preset Bundle into `bundle_dest`.

    Reads the repo's `bundle/package.json` and generated
    `bundle/cordis.patch.yml`, substituting the plugin paths into the patch,
    and writes both into `bundle_dest`. The rendered patch is validated BEFORE
    anything is written, so a broken template cannot leave a half-written
    bundle behind.
    """
    source_dir = source_root / BUNDLE_SOURCE_DIRNAME
    if not source_dir.is_dir():
        fatal(f"bundle source dir does not exist: {source_dir}\n"
              "       run `python tools/rebase_preset.py` to generate it")

    rendered = render_bundle_patch(source_dir / "cordis.patch.yml", plugins_dir, dry_run)

    if bundle_dest.exists():
        log(f"  rm    {bundle_dest}")
        if not dry_run:
            shutil.rmtree(bundle_dest)
    log(f"  cp -r {source_dir} -> {bundle_dest}")
    if dry_run:
        return
    bundle_dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_dir / "package.json", bundle_dest / "package.json")
    (bundle_dest / "cordis.patch.yml").write_text(rendered, encoding="utf-8")


def remove_legacy_preset(legacy_preset: Path, dry_run: bool) -> None:
    """Delete the DSH 0.1.x preset directory, which 0.2.0 no longer scans.

    Only our own `standard-mmx` directory is touched; the parent
    `.agent-presets/` is removed too when this was its last entry, and left
    alone otherwise so any other preset the user keeps is unaffected.
    """
    if not legacy_preset.exists():
        return
    log(f"  rm    {legacy_preset}   (DSH >= 0.2.0 does not scan .agent-presets/)")
    if dry_run:
        return
    shutil.rmtree(legacy_preset)
    parent = legacy_preset.parent
    try:
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            log(f"  rm    {parent}   (now empty)")
    except OSError:
        pass


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
    p.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the post-install tools/verify-contract.mjs probe.",
    )
    return p.parse_args(argv)


def verify_install(source_root: Path, target_home: Path, dry_run: bool) -> None:
    """Run tools/verify-contract.mjs against the install we just wrote.

    That probe re-compiles both tool definitions with the installed
    `@deepseek-ai/dsh-tools` and checks the model-facing schemas, so a DSH
    upgrade that changed the schema compiler is caught here rather than by a
    broken tool call later. Best-effort: a missing node or a missing probe
    prints a note instead of failing an otherwise good install.
    """
    probe = source_root / "tools" / "verify-contract.mjs"
    if not probe.is_file():
        log("  note  tools/verify-contract.mjs not found; skipping contract probe")
        return
    node = shutil.which("node")
    if node is None:
        log("  note  node not on PATH; skipping contract probe")
        return
    dsh_home = target_home / DSH_HOME_SUBDIR
    log(f"  run   node {probe.relative_to(source_root)} --dsh-home {dsh_home}")
    if dry_run:
        return
    result = subprocess.run(
        [node, str(probe), "--dsh-home", str(dsh_home)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    tail = (result.stdout or "").strip().splitlines()
    for line in tail[-6:]:
        log(f"        {line}")
    if result.returncode != 0:
        log("")
        log("  WARNING: the contract probe reported failures (see above).")
        log("           The plugins are installed, but their model-facing tool")
        log("           schemas did not verify against this DSH version. Fix the")
        log("           plugin sources, re-run install.py, and do not rely on the")
        log("           tools until the probe passes.")
        return
    log("  ok    contract probe passed")


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

    plugins_dir, bundle_dest, legacy_preset, dsh_node_modules, dsh_package = \
        resolve_paths(source_root, target_home)

    log(f"DSH home:    {target_home}")
    log(f"DSH install: {dsh_package if dsh_package else '(not found — junction will be skipped)'}")
    log(f"DSH version: {read_dsh_version(dsh_package)}")
    log(f"Source root: {source_root}")
    log(f"Platform:    {sys.platform}  ({'windows' if is_windows() else 'posix'})")
    log(f"Mode:        {'DRY-RUN' if args.dry_run else 'APPLY'}")
    log("")

    # 0. Validate the bundle template BEFORE writing anything (see module
    #    docstring): a bad template must not leave plugins copied but no preset.
    log("[0/4] validate bundle template")
    rendered = render_bundle_patch(
        source_root / BUNDLE_SOURCE_DIRNAME / "cordis.patch.yml", plugins_dir, args.dry_run
    )
    log(f"  ok    patch renders, {rendered.count(chr(10))} lines, placeholders substituted")
    log("")

    # 1. Plugin source dirs.
    log("[1/4] plugin sources -> ~/.dsh/plugins/")
    for src_name in PLUGIN_SOURCES:
        plugin_dest = plugins_dir / src_name
        install_dir(source_root / src_name, plugin_dest, args.dry_run)
        # Per-plugin extras: Node module bridge + Python deps recommendation.
        if src_name in PLUGIN_NODE_BRIDGE:
            bridge_plugin_node_modules(
                plugin_dest, dsh_node_modules,
                PLUGIN_NODE_BRIDGE[src_name], args.dry_run,
            )
        if src_name in PLUGIN_PYTHON_DEPS:
            recommend_python_deps(src_name, PLUGIN_PYTHON_DEPS[src_name], args.dry_run)
    log("")

    # 2. The preset Bundle, plus removal of the DSH 0.1.x preset directory.
    log("[2/4] agent preset bundle")
    install_bundle(source_root, bundle_dest, plugins_dir, args.dry_run)
    remove_legacy_preset(legacy_preset, args.dry_run)
    log("")

    # 3. Contract probe (the part that catches a DSH schema-compiler change).
    log("[3/4] contract probe")
    if args.no_verify:
        log("  skip  --no-verify")
    else:
        verify_install(source_root, target_home, args.dry_run)
    log("")

    # 4. The one step only plugin_manager may perform.
    log("[4/4] bundle install (run this once)")
    if args.dry_run:
        log(f"  would print the install_bundle call for: {bundle_dest}")
    else:
        log("  Since DSH 0.2.0 an agent preset is a Loader row, and only")
        log("  plugin_manager may install a Bundle into this profile:")
        log("")
        log("    plugin_manager action=install_bundle")
        log(f"                     target={bundle_dest}")
        log("")
        log("  Or use the Plugins page in the sidebar.")
    log("")
    log("Done.")
    log("After the bundle is installed: Settings -> Agent preset ->")
    log("'Standard (mmx)', then start a NEW session (the tool schema is")
    log("assembled at session start; an existing session keeps the old one).")
    log("")
    log("If the DSH version above is newer than the one this preset mirrors,")
    log("re-derive it first:  python tools/rebase_preset.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())