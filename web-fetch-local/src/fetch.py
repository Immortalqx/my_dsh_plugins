#!/usr/bin/env python3
"""fetch.py — DSH web_fetch_local v2 backing implementation.

Invoked by the Node.js plugin (src/index.mjs) via subprocess. Responsibility:

  - HTTP fetch with SSRF defense (any public host allowed; loopback/private blocked).
  - Content-Type gate (rejects PDF, images, archives, etc., with a friendly hint).
  - Charset decoding.
  - **Raw HTML preservation**: returns the decoded HTML to the Node side as-is;
    HTML→Markdown conversion is done there with Turndown + GFM (reusing DSH's
    installed packages — see install.py junction bridge).
  - **Structured data extraction**: optional `extruct` dependency extracts
    JSON-LD / Microdata / OpenGraph / RDFa / Microformat; soft-degrades to
    `metadata: null` when not installed.
  - Writes the raw body to a cache file under `~/.dsh/cache/web-fetch/` so
    Node side can read it back for line-layout computation.

Response JSON shape (v2):
    ok                  bool
    url                 str   # final URL after redirects
    status              int   # HTTP status code
    content_type        str   # mime type (stripped of params)
    bytes               int   # bytes actually read
    total_bytes         int|None
    truncated           bool  # more content existed
    body                str   # decoded HTML (or raw text for non-HTML ctype)
    body_kind           str   # "html" | "text"
    title               str   # <title> for HTML, "" for others
    metadata            dict|None  # JSON-LD/OG/Microdata/RDFa; null when absent
    metadata_kind       str   # "json-ld"|"opengraph"|"mixed"|"none"
    extruct_available   bool  # True if extruct was importable
    full_path           str   # path to cache file containing raw body
    error               str   # only present when ok=False
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import ipaddress
import json
import os
import socket
import sys
import urllib.error
import urllib.request
import zlib
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

# Import brotli if available (some servers send br-encoded responses).
try:
    import brotli  # type: ignore
    _BROTLI_AVAILABLE = True
except ImportError:
    brotli = None  # type: ignore
    _BROTLI_AVAILABLE = False

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


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DSH-Tool/0.4 (+web_fetch_local; raw-html; turndown-md)"

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
    """Human-readable name for a rejected ctype, or "" if unrecognized."""
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


# ---- SSRF gate -----------------------------------------------------------

def _build_ssrfsafe_opener():
    """Build an opener that re-runs `is_blocked` on every redirect target."""
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
    """True if host is a literal IP in loopback/private/link-local/reserved."""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
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


# ---- structured data extraction (soft dep) --------------------------------

_EXSTRUCT_AVAILABLE = False
_EXSTRUCT_IMPORT_ERROR = ""
try:
    import extruct  # type: ignore
    _EXSTRUCT_AVAILABLE = True
except ImportError as e:
    _EXSTRUCT_IMPORT_ERROR = str(e)


def _extract_metadata(html: str, base_url: str) -> tuple[dict | None, str]:
    """Run extruct over the raw HTML and return (data, kind).

    `data` is a dict with one entry per syntax; `kind` is a short label the
    Node side can surface to the model so it knows what was found.

    When extruct is not installed, returns (None, "none") — the Node side
    still gets a `metadata: null` in its result, and the user can install
    `extruct` later (see install.py).
    """
    if not _EXSTRUCT_AVAILABLE:
        return None, "none"
    try:
        data = extruct.extract(
            html,
            base_url=base_url,
            syntaxes=["json-ld", "microdata", "opengraph", "microformat", "rdfa"],
        )
    except Exception as e:  # noqa: BLE001 — never let a parser crash the fetch
        return {"error": f"{type(e).__name__}: {e}"}, "none"

    has_jsonld = bool(data.get("json-ld"))
    has_og = bool(data.get("opengraph"))
    has_other = any(data.get(k) for k in ("microdata", "microformat", "rdfa"))
    if has_jsonld and (has_og or has_other):
        kind = "mixed"
    elif has_jsonld:
        kind = "json-ld"
    elif has_og:
        kind = "opengraph"
    elif has_other:
        kind = "mixed"
    else:
        kind = "none"
    return data, kind


# ---- <title> capture (stdlib fallback when metadata absent) --------------

class _TitleParser(HTMLParser):
    """Tiny HTMLParser that captures the first <title> element's text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_title = False
        self._buf = ""

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
            self._buf = ""

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self._buf += data

    @property
    def title(self) -> str:
        return " ".join(self._buf.split())


def _extract_title(html: str) -> str:
    p = _TitleParser()
    try:
        p.feed(html)
    except Exception:
        return ""
    return p.title


# ---- cache + line layout ------------------------------------------------

def _dsh_home() -> str:
    return os.environ.get("DSH_HOME") or os.path.expanduser("~/.dsh")


def _cache_dir() -> str:
    return os.path.join(_dsh_home(), "cache", "web-fetch")


def _cache_filename(url: str) -> str:
    """Hash-based filename. v2 caches raw HTML; Node side rewrites it to
    Markdown+Resources+Metadata after Turndown runs."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"web-fetch_{digest}.txt"


def _write_cache_raw(url: str, body: str) -> tuple[str, str | None]:
    try:
        cache_dir = _cache_dir()
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, _cache_filename(url))
        with open(cache_path, "w", encoding="utf-8", errors="replace") as fp:
            fp.write(body)
        return cache_path, None
    except OSError as e:
        return "", f"{type(e).__name__}: {e}"


# ---- transport decoding --------------------------------------------------

def _decode_transport(raw: bytes, content_encoding: str | None) -> bytes:
    """Decompress gzip / deflate / brotli transport-encoded bytes.

    urllib does NOT auto-decompress even when we send Accept-Encoding —
    we have to do it ourselves. Identity / missing header → bytes pass
    through unchanged.
    """
    if not content_encoding:
        return raw
    ce = content_encoding.lower().strip()
    if ce == "identity" or ce == "":
        return raw
    if ce == "gzip" or ce == "x-gzip":
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError, zlib.error) as e:
            raise ValueError(f"gzip decompress failed: {e}") from e
    if ce == "deflate":
        try:
            # Some servers send raw deflate, others zlib-wrapped.
            try:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                return zlib.decompress(raw)
        except zlib.error as e:
            raise ValueError(f"deflate decompress failed: {e}") from e
    if ce == "br":
        if not _BROTLI_AVAILABLE:
            raise ValueError("brotli-encoded response but `brotli` package not installed (pip install brotli)")
        try:
            return brotli.decompress(raw)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"brotli decompress failed: {e}") from e
    # Unknown encoding — pass through; the charset decode below will report
    # the problem if the result is not valid text.
    return raw


def _decode_body(raw: bytes, charset: str | None) -> str:
    """Decode response body bytes to text using the Content-Type charset.

    Transport decoding (gzip/deflate/br) happens before this in
    `_decode_transport`; charset decoding happens here.
    """
    encoding = charset or "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


# ---- main entry ---------------------------------------------------------

def fetch(url: str, max_bytes: int, timeout: int) -> dict:
    blocked, reason = is_blocked(url)
    if blocked:
        return {"ok": False, "url": url, "error": f"blocked: {reason}"}

    if max_bytes < 1024:
        return {"ok": False, "url": url, "error": f"max_bytes must be >= 1024, got {max_bytes}"}
    if max_bytes > 64_000_000:
        return {"ok": False, "url": url, "error": f"max_bytes must be <= 64000000, got {max_bytes}"}

    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.5",
        "Accept-Encoding": "gzip, deflate",
    })

    opener = _build_ssrfsafe_opener()

    try:
        with opener.open(req, timeout=timeout) as resp:
            ctype_header = resp.headers.get_content_type() or ""
            ctype = ctype_header.split(";", 1)[0].strip().lower()
            if ctype not in ALLOWED_CONTENT_TYPES:
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
            # urllib does NOT auto-decompress; do gzip/deflate/br manually.
            content_encoding = resp.headers.get("Content-Encoding")
            try:
                decoded = _decode_transport(raw, content_encoding)
            except ValueError as e:
                return {
                    "ok": False,
                    "url": resp.geturl(),
                    "content_type": ctype,
                    "error": f"{e}",
                }
            text = _decode_body(decoded, charset)

            final_url = resp.geturl()

            # Title + metadata extraction (only meaningful for HTML).
            page_title = ""
            metadata = None
            metadata_kind = "none"
            if ctype == "text/html":
                page_title = _extract_title(text)
                metadata, metadata_kind = _extract_metadata(text, final_url)

            more_exists = (
                (total_bytes is not None and len(raw) < total_bytes)
                or truncated_local
            )

            # v2 cache = raw HTML (or raw text for non-HTML).
            # Node side rewrites it to Markdown+Resources+Metadata after Turndown.
            cache_path, cache_error = _write_cache_raw(url, text)

            return {
                "ok": True,
                "title": page_title,
                "url": final_url,
                "status": resp.status,
                "bytes": len(raw),
                "total_bytes": total_bytes,
                "truncated": more_exists,
                "content_type": ctype,
                "body_kind": "html" if ctype == "text/html" else "text",
                "body": text,
                "metadata": metadata,
                "metadata_kind": metadata_kind,
                "extruct_available": _EXSTRUCT_AVAILABLE,
                "extruct_import_error": _EXSTRUCT_IMPORT_ERROR if not _EXSTRUCT_AVAILABLE else "",
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
    ap = argparse.ArgumentParser(description="DSH web_fetch_local v2 backing")
    ap.add_argument("--url", required=True, help="http(s) URL to fetch")
    ap.add_argument("--max-bytes", type=int, default=64_000_000,
                    help="Hard byte cap per call (1024..64000000). Default 64000000.")
    ap.add_argument("--timeout", type=int, default=15,
                    help="Socket timeout seconds (default 15)")
    ns = ap.parse_args(argv)
    result = fetch(ns.url, ns.max_bytes, ns.timeout)
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
