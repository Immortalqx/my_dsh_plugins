#!/usr/bin/env python3
"""fetch.py — DSH web_fetch_local backing implementation.

Invoked by the Node.js plugin (src/index.mjs) via subprocess. Reads a single
URL with stdlib urllib, rejects loopback/private targets (SSRF defence),
decodes HTML to text with stdlib html.parser, writes the full extracted text
to a cache file under ~/.dsh/cache/web-fetch/, and prints a JSON object
describing the result. The Node side then renders a short preview plus a
pointer to the cache path so the model can re-read the full text via DSH's
`str_replace_editor` `view` command.

This helper does NOT maintain a public-host allowlist: anything reachable
on the public internet is OK to fetch. The web_search we replace
(mmx-cli, MiniMax OAuth) already filters search results, so over-blocking
on the fetch side would lock the agent out of legitimate pages.

Args:
    --url <url>                  Required. http(s) URL.
    --max-bytes <int>            Default 500_000. Hard byte cap on the raw
                                 HTTP body. Model may pass up to 2_000_000.
    --timeout <sec>              Default 15. urllib timeout.

Response JSON shape:
    {
        "ok": true|false,
        "url": final_url (after redirects),
        "status": http_status,
        "bytes": raw bytes returned,
        "total_bytes": full content length if server told us, else null,
        "truncated": true if server has more than we read,
        "content_type": response content-type,
        "text_chars": total extracted text length in chars,
        "preview_chars": chars kept in this response (vs the cache file),
        "preview": first N chars of the extracted text,
        "full_path": absolute path to the cached full text file
    }

Exit code is always 0 on success (errors come back as JSON `ok: false`).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlparse

# Force UTF-8 on stdout/stderr. Windows defaults to the active OEM code page
# (often GBK on CN machines), which crashes on \xa0 (nbsp) inside page text.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE1
        pass

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DSH-Tool/0.3 (+web_fetch_local; file-cache)"

# SSRF blocklist — only loopback / private hosts are denied. Public hosts
# pass through; web_search (mmx-cli, MiniMax OAuth) already filters search
# results, so over-blocking on fetch would just lock the agent out of
# legitimate pages.
PRIVATE_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
PRIVATE_SUFFIXES = (".local", ".internal", ".lan")

# How many chars of the extracted text to ship in the tool response. The
# full text is always written to a cache file the model can re-read with
# the read tool. This is the only "truncation" the model sees — the cache
# file is never truncated.
PREVIEW_CHARS = 2_000

SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside",
             "noscript", "iframe", "form", "svg", "canvas"}


def is_blocked(url: str) -> tuple[bool, str]:
    """Return (blocked, reason). Reason is non-empty only when blocked.

    Blocks loopback + RFC1918-style private-host targets. Anything public
    is allowed; the SSRF defence here is intentionally minimal."""
    try:
        parsed = urlparse(url)
    except ValueError as e:
        return True, f"url parse failed: {e}"
    if parsed.scheme not in ("http", "https"):
        return True, f"scheme must be http(s), got {parsed.scheme!r}"
    if not parsed.hostname:
        return True, "no hostname"
    host = parsed.hostname.lower()
    if host in PRIVATE_HOSTS:
        return True, "loopback host blocked (SSRF defence)"
    if any(host.endswith(suffix) for suffix in PRIVATE_SUFFIXES):
        return True, "private-host suffix blocked (SSRF defence)"
    return False, ""


class TextExtractor(HTMLParser):
    """Minimal HTML → visible-text extractor. Strips script/style/nav/etc."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip == 0 and data.strip():
            self.parts.append(data)


def _parse_content_range(cr: str) -> int | None:
    """Parse 'bytes 0-499/1234' → 1234. Return None on failure."""
    if not cr or "/" not in cr:
        return None
    tail = cr.rsplit("/", 1)[-1].strip()
    if tail == "*":
        return None
    try:
        return int(tail)
    except ValueError:
        return None


def _dsh_home() -> str:
    """Resolve DSH's user home directory in a cross-platform way."""
    return os.environ.get("DSH_HOME") or os.path.expanduser("~/.dsh")


def _cache_dir() -> str:
    return os.path.join(_dsh_home(), "cache", "web-fetch")


def _cache_filename(url: str) -> str:
    """Per-URL hash-based filename. The same URL always maps to the same
    file, so repeated fetches overwrite in place instead of leaving stale
    duplicates. Format: web-fetch_<url-hash8>.txt.

    History (per-call snapshots) is not preserved; if you need that,
    write your own wrapper that copies the cache file elsewhere first.
    """
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"web-fetch_{digest}.txt"


def fetch(url: str, max_bytes: int, timeout: int) -> dict:
    blocked, reason = is_blocked(url)
    if blocked:
        return {"ok": False, "url": url, "error": f"blocked: {reason}"}

    if max_bytes < 1024:
        return {"ok": False, "url": url, "error": f"max_bytes must be >= 1024, got {max_bytes}"}
    if max_bytes > 2_000_000:
        return {"ok": False, "url": url, "error": f"max_bytes must be <= 2000000, got {max_bytes}"}

    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.5",
    })

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes + 1)
            truncated_local = len(raw) > max_bytes
            if truncated_local:
                raw = raw[:max_bytes]

            # total_bytes discovery — best effort, may be None
            total_bytes: int | None = None
            cl = resp.headers.get("Content-Length")
            if cl:
                try:
                    total_bytes = int(cl)
                except ValueError:
                    pass

            charset = resp.headers.get_content_charset() or "utf-8"
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            ctype = resp.headers.get_content_type() or ""

            body_text = text
            if "text/html" in ctype:
                extractor = TextExtractor()
                try:
                    extractor.feed(text)
                    # Preserve block structure: each extracted chunk becomes its
                    # own line so the model's read tool can paginate by line
                    # number (`view_range=[start, end]`) instead of seeing one
                    # giant single-line blob.
                    body_text = "\n".join(
                        " ".join(part.split())  # collapse internal whitespace
                        for part in extractor.parts
                        if part.strip()
                    )
                except Exception:
                    body_text = text  # fall back rather than crash

            # Did we get all of it?
            more_exists = False
            if total_bytes is not None:
                more_exists = len(raw) < total_bytes
            elif truncated_local:
                more_exists = True

            # Persist the full extracted text to a cache file. We do this
            # even when `more_exists` is true so the model can `view` the
            # partial body without re-fetching.
            cache_dir = _cache_dir()
            try:
                os.makedirs(cache_dir, exist_ok=True)
                cache_path = os.path.join(cache_dir, _cache_filename(url))
                with open(cache_path, "w", encoding="utf-8", errors="replace") as fp:
                    fp.write(body_text)
            except OSError as e:
                cache_path = ""
                cache_error = f"{type(e).__name__}: {e}"
            else:
                cache_error = None

            preview = body_text[:PREVIEW_CHARS]

            return {
                "ok": True,
                "url": resp.geturl(),
                "status": resp.status,
                "bytes": len(raw),
                "total_bytes": total_bytes,
                "truncated": more_exists,
                "content_type": ctype,
                "text_chars": len(body_text),
                "preview_chars": len(preview),
                "preview": preview,
                "full_path": cache_path,
                "cache_error": cache_error,
            }
    except urllib.error.HTTPError as e:
        return {"ok": False, "url": url, "status": e.code,
                "error": f"HTTP {e.code}: {e.reason}"}
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if isinstance(e.reason, socket.timeout):
            return {"ok": False, "url": url, "error": f"timed out after {timeout}s"}
        return {"ok": False, "url": url, "error": f"URLError: {reason}"}
    except socket.timeout:
        return {"ok": False, "url": url, "error": f"timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "url": url, "error": f"{type(e).__name__}: {e}"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="DSH web_fetch_local backing")
    ap.add_argument("--url", required=True, help="http(s) URL to fetch")
    ap.add_argument("--max-bytes", type=int, default=500_000,
                    help="Hard byte cap per call (1024..2000000). Default 500000.")
    ap.add_argument("--timeout", type=int, default=15,
                    help="Socket timeout seconds (default 15)")
    ns = ap.parse_args(argv)
    result = fetch(ns.url, ns.max_bytes, ns.timeout)
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))