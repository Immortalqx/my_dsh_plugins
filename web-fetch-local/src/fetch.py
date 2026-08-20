#!/usr/bin/env python3
"""fetch.py — DSH web_fetch_local backing implementation.

Invoked by the Node.js plugin (src/index.mjs) via subprocess. Reads a URL
with stdlib urllib, decodes HTML to text, extracts <a href> + <img src>
resources, writes the body + Resources to a cache file, reads it back to
compute line numbers, and prints a JSON result.

Args:
    --url <url>          Required. http(s) URL.
    --max-bytes <int>    Default 500_000. Hard byte cap on the raw body.
    --timeout <sec>      Default 15. urllib timeout.

Response JSON shape: see fetch() return value.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

# Windows defaults to the active OEM code page (often GBK on CN machines),
# which crashes on \xa0 (nbsp) inside page text.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DSH-Tool/0.3 (+web_fetch_local; file-cache)"

# Content types we know how to extract text from. Anything else (PDF, images,
# archives, binary, XML, ...) is rejected with a friendly format hint so the
# agent can pick another tool.
ALLOWED_CONTENT_TYPES = frozenset({
    "text/html",
    "text/plain",
    "application/json",
    "text/markdown",
    "text/x-markdown",
})


def _format_kind(ctype: str) -> str:
    """Human-readable name for a rejected ctype, or "" if unrecognized.

    PDF and image/* get specific names so the agent can route them to a
    download tool. Archives and generic binary also get named. Anything
    else falls through (the caller still surfaces the raw ctype).
    """
    if not ctype:
        return ""
    if ctype == "application/pdf":
        return "PDF"
    if ctype.startswith("image/"):
        image_names = {
            "image/png": "PNG image",
            "image/jpeg": "JPEG image",
            "image/gif": "GIF image",
            "image/webp": "WebP image",
            "image/svg+xml": "SVG image",
            "image/bmp": "BMP image",
            "image/tiff": "TIFF image",
            "image/x-icon": "icon",
            "image/ico": "icon",
            "image/vnd.microsoft.icon": "icon",
        }
        return image_names.get(ctype, "image")
    archive_names = {
        "application/zip": "ZIP archive",
        "application/x-zip-compressed": "ZIP archive",
        "application/x-7z-compressed": "7-Zip archive",
        "application/x-rar-compressed": "RAR archive",
        "application/gzip": "GZIP archive",
        "application/x-gzip": "GZIP archive",
        "application/x-tar": "TAR archive",
        "application/x-bzip2": "BZIP2 archive",
        "application/vnd.rar": "RAR archive",
    }
    if ctype in archive_names:
        return archive_names[ctype]
    if ctype == "application/octet-stream":
        return "binary"
    return ""

PRIVATE_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "0"}
PRIVATE_SUFFIXES = (".local", ".internal", ".lan")

# Chars of the extracted text shipped inline. Full text lives in cache.
PREVIEW_CHARS = 2_000

SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside",
             "noscript", "iframe", "form", "svg", "canvas"}


# ---- SSRF gate -----------------------------------------------------------

def _build_ssrfsafe_opener():
    """Build an opener that re-runs `is_blocked` on every redirect target.

    urllib's default HTTPRedirectHandler follows up to ~20 redirects without
    re-checking our SSRF gate — a server could 302 to http://127.0.0.1/ and
    bypass the gate. We override `redirect_request` (the shared hook called
    by http_error_301/302/303/307/308) to validate each Location before
    urllib follows it.
    """
    class _SafeRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            blocked, reason = is_blocked(newurl)
            if blocked:
                raise urllib.error.HTTPError(
                    req.full_url, code,
                    f"redirect target blocked: {reason}",
                    headers, fp,
                )
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return urllib.request.build_opener(_SafeRedirect())


def _ip_is_loopback_or_private(host: str) -> bool:
    """True if host is a literal IP in loopback/private/link-local/reserved.

    Handles IPv4 and IPv6, including IPv4-mapped IPv6 (`::ffff:127.0.0.1`)
    which would otherwise bypass a string-only host check.
    """
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    # IPv4-mapped IPv6: ::ffff:127.0.0.1 etc.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        v4 = ip.ipv4_mapped
    elif isinstance(ip, ipaddress.IPv4Address):
        v4 = ip
    else:
        v4 = None
    if v4 is not None and (v4.is_loopback or v4.is_private
                           or v4.is_link_local or v4.is_reserved):
        return True
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved


def is_blocked(url: str) -> tuple[bool, str]:
    """Return (blocked, reason). Reason is non-empty only when blocked."""
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
    if _ip_is_loopback_or_private(host):
        return True, "loopback/private IP blocked (SSRF defence)"
    return False, ""


# ---- HTML → text + resources ---------------------------------------------

class TextExtractor(HTMLParser):
    """HTML → visible-text extractor + <a>/<img> resource collector.

    URLs are absolutized against `base_url`. mailto:/javascript:/data:/tel:
    are dropped. Same-URL duplicates are dropped (first wins).
    """

    _JUNK_PREFIXES = ("mailto:", "javascript:", "data:", "tel:")
    _RESOURCE_CAP = 200  # per-section safety cap

    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        # Per-tag skip depth. Using a single counter broke on mismatched
        # closing tags (e.g., `<script>...</style>`) — the close would
        # decrement the wrong way and bleed <script> contents into body_text.
        self._skip_depth: dict[str, int] = {}
        self._links: list[tuple[str, str]] = []   # (text, abs_url)
        self._images: list[tuple[str, str]] = []  # (alt, abs_url)
        self._pending_link: str | None = None
        self._link_text_buf: str = ""
        self._seen_urls: set[str] = set()
        self._base_url = base_url
        # Title capture: <title> text isn't skipped (some titles carry
        # useful info like "Article | Site Name") but also doesn't belong in
        # body parts, so we collect it separately.
        self.title: str = ""
        self._in_title: bool = False
        self._title_buf: str = ""

    def _is_real_url(self, url: str) -> bool:
        if not url:
            return False
        low = url.lower()
        return not any(low.startswith(p) for p in self._JUNK_PREFIXES)

    def _absolutize(self, url: str) -> str:
        if not self._base_url or url.startswith(("#", "?")):
            return url
        return urljoin(self._base_url, url)

    def _in_skip(self) -> bool:
        """True if we're inside any skip-tag region."""
        return any(d > 0 for d in self._skip_depth.values())

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "title":
            self._in_title = True
            self._title_buf = ""
            return
        if tag in SKIP_TAGS:
            self._skip_depth[tag] = self._skip_depth.get(tag, 0) + 1
            return
        if tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if not self._is_real_url(href):
                self._pending_link = None
                return
            abs_url = self._absolutize(href)
            if abs_url in self._seen_urls:
                self._pending_link = None
            else:
                self._pending_link = abs_url
                self._link_text_buf = ""
                self._seen_urls.add(abs_url)
        elif tag == "img":
            src = (dict(attrs).get("src") or "").strip()
            if not self._is_real_url(src):
                return
            abs_url = self._absolutize(src)
            if abs_url not in self._seen_urls:
                alt = (dict(attrs).get("alt") or "").strip()
                self._images.append((alt, abs_url))
                self._seen_urls.add(abs_url)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self.title = " ".join(self._title_buf.split())
            self._in_title = False
            return
        if tag in SKIP_TAGS:
            depth = self._skip_depth.get(tag, 0)
            if depth > 0:
                self._skip_depth[tag] = depth - 1
            # Mismatched close (e.g., </style> without matching <style>): no-op.
        elif tag == "a" and self._pending_link is not None:
            text = " ".join(self._link_text_buf.split())
            self._links.append((text or self._pending_link, self._pending_link))
            self._pending_link = None
            self._link_text_buf = ""

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_buf += data
            return
        if self._in_skip() or not data.strip():
            return
        if self._pending_link is not None:
            self._link_text_buf += data
        else:
            self.parts.append(data)

    def resources_section(self) -> str:
        """Render collected links + images as a markdown block. Returns ""
        when there are no resources, so the caller can concatenate unconditionally."""
        total = len(self._links) + len(self._images)
        if total == 0:
            return ""
        lines: list[str] = [
            f"## Resources ({len(self._links)} links, {len(self._images)} images)",
            "",
        ]
        if self._links:
            lines.append("### Links")
            lines.append("")
            for text, url in self._links[: self._RESOURCE_CAP]:
                lines.append(f"- [{text}]({url})")
            lines.append("")
        if self._images:
            lines.append("### Images")
            lines.append("")
            for alt, url in self._images[: self._RESOURCE_CAP]:
                lines.append(f"- ![{alt}]({url})")
            lines.append("")
        if total > self._RESOURCE_CAP:
            lines.append(f"(... and {total - self._RESOURCE_CAP} more)")
            lines.append("")
        return "\n".join(lines)

    @property
    def links_count(self) -> int:
        return len(self._links)

    @property
    def images_count(self) -> int:
        return len(self._images)


# ---- cache + line layout ------------------------------------------------

def _count_lines(s: str) -> int:
    """Lines in `s`. "" → 0, "a" → 1, "a\\n" → 1, "a\\nb" → 2."""
    if not s:
        return 0
    return s.count("\n") + (0 if s.endswith("\n") else 1)


def _dsh_home() -> str:
    return os.environ.get("DSH_HOME") or os.path.expanduser("~/.dsh")


def _cache_dir() -> str:
    return os.path.join(_dsh_home(), "cache", "web-fetch")


def _cache_filename(url: str) -> str:
    """Hash-based filename so repeated fetches overwrite the cache file in
    place. Format: web-fetch_<url-hash8>.txt."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"web-fetch_{digest}.txt"


def _write_cache(url: str, body_text: str, resources_block: str) -> tuple[str, str | None]:
    """Write body + Resources to the cache file. Returns (path, error).

    Caller is responsible for any trailing-newline normalization on
    `body_text` (see fetch()).
    """
    try:
        cache_dir = _cache_dir()
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, _cache_filename(url))
        payload = body_text + ("\n\n" + resources_block if resources_block else "")
        with open(cache_path, "w", encoding="utf-8", errors="replace") as fp:
            fp.write(payload)
        return cache_path, None
    except OSError as e:
        return "", f"{type(e).__name__}: {e}"


def _read_cache_layout(body_text: str, resources_block: str,
                       cache_path: str) -> tuple[int, int, int | None, int]:
    """Read the cache file back and compute (body_lines, resources_lines,
    resources_start_line, total_lines). Authoritative — uses the actual
    file, not a hand-mirrored calculation. body_text is the in-memory body,
    used as fallback when the file is unreadable."""
    body_lines = _count_lines(body_text)
    if not cache_path or not os.path.exists(cache_path):
        return body_lines, 0, None, body_lines
    try:
        with open(cache_path, "r", encoding="utf-8", errors="replace") as fp:
            file_lines = fp.read().splitlines()
    except OSError:
        # Read-back failed; fall back to in-memory computation.
        if not resources_block:
            return body_lines, 0, None, body_lines
        resources_lines = _count_lines(resources_block)
        # payload joins body with "\n\n" (no trailing \n on body, no leading
        # \n on resources), so the separator contributes one blank line.
        return body_lines, resources_lines, body_lines + 2, body_lines + 1 + resources_lines

    total_lines = len(file_lines)
    if not resources_block:
        return body_lines, 0, None, total_lines
    # Match the heading emitted by resources_section():
    # "## Resources (N links, M images)"
    marker_idx = next(
        (i for i, ln in enumerate(file_lines)
         if ln.startswith("## Resources") and "links" in ln and "images" in ln),
        None
    )
    if marker_idx is None:
        # resources_block was non-empty but the marker line wasn't found
        # (shouldn't happen — resources_section() always emits it).
        resources_lines = _count_lines(resources_block)
        return body_lines, resources_lines, total_lines - resources_lines + 1, total_lines
    resources_start_line = marker_idx + 1  # 1-indexed
    resources_lines = total_lines - marker_idx
    return body_lines, resources_lines, resources_start_line, total_lines


# ---- main entry ---------------------------------------------------------

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

    opener = _build_ssrfsafe_opener()

    try:
        with opener.open(req, timeout=timeout) as resp:
            # Content-Type gate (headers are available before reading the body):
            # strip params like "; charset=utf-8" before matching the allowlist.
            ctype_header = resp.headers.get_content_type() or ""
            ctype = ctype_header.split(";", 1)[0].strip().lower()
            if ctype not in ALLOWED_CONTENT_TYPES:
                # Friendly format hint for the agent (PDF / PNG image / GZIP archive /
                # binary / ...). Falls back to the raw ctype, or "format not allowed"
                # if the server didn't return a Content-Type at all.
                if not ctype:
                    error = "format not allowed (server did not return Content-Type)"
                else:
                    kind = _format_kind(ctype)
                    if kind:
                        error = f"unsupported content type: {kind} ({ctype})"
                    else:
                        error = f"unsupported content type ({ctype})"
                return {
                    "ok": False,
                    "url": resp.geturl(),
                    "content_type": ctype,
                    "error": error,
                }

            raw = resp.read(max_bytes + 1)
            truncated_local = len(raw) > max_bytes
            if truncated_local:
                raw = raw[:max_bytes]

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

            body_text = text
            resources_block = ""
            links_count = 0
            images_count = 0
            page_title = ""
            if "text/html" in ctype:
                # base_url lets <a href="/x"> resolve to absolute URLs.
                extractor = TextExtractor(base_url=resp.geturl())
                try:
                    extractor.feed(text)
                    # One extracted chunk per line so the model can paginate
                    # via view_range=[start, end].
                    body_text = "\n".join(
                        " ".join(part.split())
                        for part in extractor.parts
                        if part.strip()
                    )
                    resources_block = extractor.resources_section()
                    links_count = extractor.links_count
                    images_count = extractor.images_count
                    page_title = extractor.title
                except Exception:
                    # Parser crashed; fall back to raw text + no resources.
                    body_text = text
                    resources_block = ""

            more_exists = (
                (total_bytes is not None and len(raw) < total_bytes)
                or truncated_local
            )

            # Strip trailing newlines once so body_lines (computed from
            # body_text) matches what's actually on disk after _write_cache.
            body_text = body_text.rstrip("\n")

            cache_path, cache_error = _write_cache(url, body_text, resources_block)
            body_lines, resources_lines, resources_start_line, total_lines = _read_cache_layout(
                body_text, resources_block, cache_path
            )

            preview = body_text[:PREVIEW_CHARS]

            return {
                "ok": True,
                "title": page_title,
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
                "body_lines": body_lines,
                "resources_lines": resources_lines,
                "resources_start_line": resources_start_line,
                "total_lines": total_lines,
                "links_count": links_count,
                "images_count": images_count,
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
