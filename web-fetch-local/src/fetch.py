#!/usr/bin/env python3
"""fetch.py — DSH web_fetch_local backing implementation.

Invoked by the Node.js plugin (src/index.mjs) via subprocess. Reads a single
URL with stdlib urllib, rejects loopback/private targets (SSRF defence),
decodes HTML to text with stdlib html.parser, and prints one JSON object
to stdout for the Node side to capture.

This helper does NOT maintain a public-host allowlist: anything reachable
on the public internet is OK to fetch. The web_search we replace
(MiniMax OAuth / mmx-cli) already filters search results, so over-blocking
on the fetch side would lock the agent out of legitimate pages.

Args:
    --url <url>                  Required. http(s) URL.
    --offset-bytes <int>         Default 0. Byte offset for HTTP Range
                                 request — used for paginating through
                                 long pages that exceed --max-bytes.
    --max-bytes <int>            Default 500_000. Hard byte cap per call.
                                 Model may pass up to 2_000_000 for very
                                 large pages, or smaller for snippets.
    --timeout <sec>              Default 15. urllib timeout.

Response JSON shape:
    {
        "ok": true|false,
        "url": final_url (after redirects),
        "status": http_status,
        "bytes": bytes actually returned in this chunk,
        "offset_bytes": where this chunk started (echoed back),
        "total_bytes": full content length if server told us, else null,
        "truncated": true if more content exists beyond this chunk,
        "content_type": response content-type,
        "text": extracted text body (HTML stripped if applicable)
    }

Exit code is always 0 on success (errors come back as JSON `ok: false`).
"""
from __future__ import annotations

import argparse
import json
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

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DSH-Tool/0.3 (+web_fetch_local; range-aware)"

# SSRF blocklist — only loopback / private hosts are denied. Public hosts
# pass through; web_search (mmx-cli, MiniMax OAuth) already filters search
# results, so over-blocking on fetch would just lock the agent out of
# legitimate pages.
PRIVATE_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
PRIVATE_SUFFIXES = (".local", ".internal", ".lan")

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


def fetch(url: str, offset_bytes: int, max_bytes: int, timeout: int) -> dict:
    blocked, reason = is_blocked(url)
    if blocked:
        return {"ok": False, "url": url, "error": f"blocked: {reason}"}

    if offset_bytes < 0:
        return {"ok": False, "url": url, "error": f"offset_bytes must be >= 0, got {offset_bytes}"}
    if max_bytes < 1024:
        return {"ok": False, "url": url, "error": f"max_bytes must be >= 1024, got {max_bytes}"}
    if max_bytes > 2_000_000:
        return {"ok": False, "url": url, "error": f"max_bytes must be <= 2000000, got {max_bytes}"}

    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.5",
    })
    # HTTP Range request — bandwidth-efficient when server supports it.
    # Servers that don't support Range ignore the header and return 200 + full content.
    if offset_bytes > 0:
        req.add_header("Range", f"bytes={offset_bytes}-{offset_bytes + max_bytes - 1}")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Some servers (notably Wikipedia and a few CDNs) IGNORE the
            # Range header and return the full body with HTTP 200. When that
            # happens our helper must slice locally — otherwise every
            # paginated call returns the same first window.
            #
            # HARD_READ_CAP bounds the worst-case memory cost of a
            # misbehaving server — anything larger means the agent picked
            # the wrong strategy and should fetch a smaller / more specific
            # URL instead.

            HARD_READ_CAP = 10 * 1024 * 1024  # 10 MB

            if offset_bytes > 0 and resp.status != 206:
                needed = offset_bytes + max_bytes + 1
                actual_read = min(needed, HARD_READ_CAP)
                raw = resp.read(actual_read)
                if offset_bytes >= len(raw):
                    return {"ok": False, "url": url, "status": resp.status,
                            "error": f"offset_bytes {offset_bytes} beyond read buffer ({len(raw)})"}
                raw = raw[offset_bytes:]
            else:
                raw = resp.read(max_bytes + 1)

            truncated_local = len(raw) > max_bytes
            if truncated_local:
                raw = raw[:max_bytes]

            # total_bytes discovery — best effort, may be None
            total_bytes: int | None = None
            cr = resp.headers.get("Content-Range")
            if cr:
                total_bytes = _parse_content_range(cr)
            if total_bytes is None:
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
                    body_text = " ".join(s.strip() for s in extractor.parts if s.strip())
                except Exception:
                    body_text = text  # fall back rather than crash

            # Did we get all of it?
            more_exists = False
            if total_bytes is not None:
                more_exists = (offset_bytes + len(raw)) < total_bytes
            elif truncated_local:
                more_exists = True

            return {
                "ok": True,
                "url": resp.geturl(),
                "status": resp.status,
                "bytes": len(raw),
                "offset_bytes": offset_bytes,
                "total_bytes": total_bytes,
                "truncated": more_exists,
                "content_type": ctype,
                "text": body_text[:50_000],
            }
    except urllib.error.HTTPError as e:
        if e.code == 416 and offset_bytes > 0:
            return {"ok": False, "url": url, "status": 416,
                    "error": "offset_bytes beyond end of resource"}
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
    ap.add_argument("--offset-bytes", type=int, default=0,
                    help="Byte offset for HTTP Range (pagination). Default 0.")
    ap.add_argument("--max-bytes", type=int, default=500_000,
                    help="Hard byte cap per call (1024..2000000). Default 500000.")
    ap.add_argument("--timeout", type=int, default=15,
                    help="Socket timeout seconds (default 15)")
    ns = ap.parse_args(argv)
    result = fetch(ns.url, ns.offset_bytes, ns.max_bytes, ns.timeout)
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))