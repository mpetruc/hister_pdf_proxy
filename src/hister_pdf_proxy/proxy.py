#!/usr/bin/env python3
"""Hister PDF-import proxy.

Transparent reverse-proxy in front of a Hister server that is burst in the
middle of PDF ingestion. Hister's built-in `asciimoo/pdf` text extraction
strips ALL whitespace (see asciimoo/hister#55), producing unsearchable one-line
text. This proxy intercepts `/api/add_pdf` requests from ANY client (our
web-research extract provider, the browser extension, the CLI, …), decodes the
base64 PDF, extracts clean spaced text locally with PyMuPDF, and forwards the
result to Hister via `/api/add` with the extracted text in `document.text` —
bypassing the broken PDF handler entirely.

Everything else is forwarded unchanged (search, document reads, config,
whatever), so non-PDF traffic is untouched.

Why `/api/add` and not `hister import file`: `import` routes a `.pdf` through
the same broken `pdfFileType` handler (pdfFileType is first in the handler list
in server/indexer/filetypes.go). Only submitting already-extracted text via
`/api/add` skips it.

Usage:
    env HISTER_PROXY_UPSTREAM=http://192.168.3.120:4433 ./hister_pdf_proxy.py \
        [--listen 0.0.0.0] [--port 4434]

Point Hister clients (our provider via WEBRESEARCH_HISTER_URL, and the browser
extension endpoint) at the proxy port instead of Hister's port.

Notes:
- The client's `Authorization: Bearer <token>` is forwarded unchanged, so no
  token handling here. Replace it or reject as you wish if you want auth here.
- The CSRF handshake is bypassed downstream via the `Origin: hister://` header
  Hister uses for CLI-originated requests (server.go withCSRF). We do NOT
  rewrite the client's Origin, so the client's own CSRF/session flow is
  unaffected.
"""
import argparse
import base64
import ipaddress
import json
import logging
import os
import ssl
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger("hister-pdf-proxy")

UPSTREAM = os.getenv("HISTER_PROXY_UPSTREAM", "")

# Fields from the client's /api/add_pdf request that map onto /api/add.
DOC_FIELDS = ("url", "title", "label", "domain")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        logger.warning("%s not an int, using %s", name, default)
        return default


# Max decoded PDF size we'll extract, in bytes (0 = unlimited). Prevents a
# runaway/huge document from exhausting memory or burning CPU indefinitely.
MAX_PDF_BYTES = _env_int("HISTER_PROXY_MAX_BYTES", 0)
# Hard wall-clock budget for a single PDF extraction, in seconds. The extraction
# runs in a subprocess so a timeout actually kills the work (CPU is freed), it
# does not abandon a thread that keeps spinning.
EXTRACT_TIMEOUT = _env_int("HISTER_PROXY_EXTRACT_TIMEOUT", 120)


# Inline worker run in a subprocess so extraction can be hard-killed on timeout.
# Args: <pdf_path> <fmt>; prints single-line JSON {text, metadata: {...}}.
_EXTRACT_WORKER = r"""
import json, sys
path, fmt = sys.argv[1], sys.argv[2]
# Use pymupdf (not fitz) to avoid the fitz deprecation warning that would
# otherwise leak to stdout and corrupt the JSON we emit.
pymupdf = __import__("pymupdf")
doc = pymupdf.open(path)
meta = {k: v for k, v in (doc.metadata or {}).items() if v}
try:
    if fmt == "markdown":
        import pymupdf.layout  # layout must precede pymupdf4llm
        import pymupdf4llm
        text = pymupdf4llm.to_markdown(path)
    else:
        parts = []
        for page in doc:
            parts.append(page.get_text())
        text = "\n".join(parts)
finally:
    doc.close()
json.dump({"text": text, "metadata": meta}, sys.stdout, ensure_ascii=False)
"""


# Raw pymupdf doc.metadata keys preserved verbatim under a "pdf" sub-map.
_PDF_META_KEYS = (
    "format", "title", "subject", "keywords", "creator", "producer",
    "creationDate", "modDate", "trapped", "encryption",
)


def extract_pdf_metadata(pdf_bytes: bytes) -> dict:
    """Extract the PDF's embedded metadata (pymupdf doc.metadata).

    Returns a flat dict of present, non-empty metadata keys (author, title,
    subject, keywords, creator, producer, dates, format, encryption). Empty
    values and the deprecated 'trapped' flag are dropped.
    """
    import fitz  # pymupdf, installed in this venv

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        meta = dict(doc.metadata or {})
    finally:
        doc.close()
    return {k: v for k, v in meta.items() if v}


def hister_metadata(pdf_meta: dict) -> dict:
    """Map extracted PDF metadata onto Hister's Document.Metadata schema.

    Hister's embedding context (indexer.go:970-984) reads `author`,
    `description`, and `keywords` from Document.Metadata, so we surface those
    top-level. The remaining raw PDF keys are preserved under a `pdf` sub-map
    so nothing is lost while staying namespaced.
    """
    m = {}
    if pdf_meta.get("author"):
        m["author"] = pdf_meta["author"]
    # pymupdf's `subject` is the abstract/description field.
    if pdf_meta.get("subject"):
        m["description"] = pdf_meta["subject"]
    if pdf_meta.get("keywords"):
        m["keywords"] = pdf_meta["keywords"]
    raw = {k: pdf_meta[k] for k in _PDF_META_KEYS if pdf_meta.get(k)}
    if raw:
        m["pdf"] = raw
    return m


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Local, whitespace-preserving PDF text extraction (PyMuPDF)."""
    import fitz  # pymupdf, installed in this venv
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    parts = []
    for page in doc:
        parts.append(page.get_text())
    doc.close()
    return "\n".join(parts)


def extract_pdf_markdown(pdf_bytes: bytes) -> str:
    """Local layout-aware PDF -> markdown via pymupdf4llm (layout mode).

    Slower and needs pymupdf4llm + pymupdf-layout in the venv, but returns
    structured markdown: real tables (pipe syntax), detected headings, list
    hierarchies, headers/footers dropped.

    Import order matters (PyMuPDF4LLM discussion #327):
        import pymupdf.layout   # first
        import pymupdf4llm      # after
    The layout backend calls pymupdf.open() with a filename, so the PDF bytes
    are staged to a temp file (scanned/scrolled-tolerant).
    """
    import os
    import tempfile
    import pymupdf.layout  # noqa: F401  (enables layout mode; must precede pymupdf4llm)
    import pymupdf4llm

    fd, path = tempfile.mkstemp(prefix="hister_pdf_", suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pdf_bytes)
        return pymupdf4llm.to_markdown(path)
    finally:
        os.unlink(path)


def extract_pdf(pdf_bytes: bytes, fmt: str) -> str:
    if fmt == "markdown":
        return extract_pdf_markdown(pdf_bytes)
    return extract_pdf_text(pdf_bytes)


def extract_pdf_timeout(pdf_bytes: bytes, fmt: str, timeout: int, max_bytes: int) -> tuple:
    """Subprocess-based extraction with a hard timeout and size guard.

    Returns (status_code, body, error). Extraction runs in a subprocess so that
    when it exceeds `timeout` the process is killed and the CPU is actually
    freed (a thread would keep spinning). On success `body` is the extracted
    text; metadata is returned separately via the caller re-reading doc.metadata
    is avoided by having the worker emit both.
    """
    if max_bytes and len(pdf_bytes) > max_bytes:
        return 413, json.dumps({"error": f"pdf too large "
                                         f"({len(pdf_bytes)} bytes > {max_bytes} limit)"}), None
    import subprocess
    import tempfile

    fd, path = tempfile.mkstemp(prefix="hister_pdf_", suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pdf_bytes)
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _EXTRACT_WORKER, path, fmt],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return 504, json.dumps({"error": f"pdf extraction exceeded "
                                             f"{timeout}s timeout (too large/complex?)"}), \
                "extraction timeout"
        if proc.returncode != 0:
            return 500, json.dumps({"error": f"pdf extraction failed: "
                                             f"{proc.stderr.strip()[:300]}"}), \
                proc.stderr.strip()[:300]
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            # Be robust to any stray stdout before the JSON (e.g. library
            # chatter): strip everything up to the first '{'.
            try:
                start = proc.stdout.index("{")
                out = json.loads(proc.stdout[start:])
            except (ValueError, json.JSONDecodeError):
                return 500, json.dumps({"error": "pdf extractor returned malformed output"}), \
                    "malformed extractor output: " + proc.stdout[:200]
        return 200, out, None
    finally:
        os.unlink(path)


def do_csrf_handshake(base_url: str, headers: dict) -> tuple:
    """Return (csrf token, cookie jar) for a downstream /api/add POST.

    GET /api/config through the same CookieJar to bind the X-Csrf-Token to the
    session, forwarding the client's auth headers (X-Access-Token and/or Cookie)
    so the session is authenticated.
    """
    import http.cookiejar as cookiejar

    jar = cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(f"{base_url}/api/config", headers=headers, method="GET")
    with opener.open(req, timeout=20) as r:
        csrf = r.headers.get("X-Csrf-Token", "")
    return csrf, opener


def _client_auth_headers(token: str, cookie: str, have_token: bool) -> dict:
    """Auth headers for the downstream request, mirroring the client's scheme.

    The extension authenticates either with an access token (X-Access-Token /
    Authorization: Bearer) OR with browser cookies (Cookie: ...) — see
    Popup.svelte: "Authenticate with a global or personal access token. Leave
    blank to use browser cookies." Trust the client's own headers verbatim so
    whichever scheme they used works downstream.
    """
    h = {}
    if have_token and token:
        h["X-Access-Token"] = token
        h["Authorization"] = f"Bearer {token}"
    if cookie:
        h["Cookie"] = cookie
    return h


def forward_pdf(base_url: str, document: dict, pdf_bytes: bytes, fmt: str = "text") -> tuple:
    """Extract PDF text locally and POST to /api/add.

    Returns (status_code, response_body, error_string).
    fmt: 'text' (fast, page.get_text) or 'markdown' (pymupdf4llm layout).
    """
    url = document.get("url", "")

    # Surface the extension-detection gap: a PDF submitted whose URL path does
    # not end in `.pdf` (e.g. arxiv /pdf/<id>). The extension's isPDFUrl() is a
    # URL-string heuristic, so these PDFs only make it here when a client that
    # DID detect them (e.g. our provider) submits them. Log it so the gap stays
    # visible.
    try:
        from urllib.parse import urlsplit
        path = urlsplit(url).path.lower()
        if not path.endswith(".pdf") and fmt == "text":
            logger.warning(
                "add_pdf for URL whose path does not end in .pdf (extension "
                "isPDFUrl heuristic may miss these): %s", url,
            )
    except Exception:  # noqa: BLE001
        pass

    status, out, err = extract_pdf_timeout(
        pdf_bytes, fmt, EXTRACT_TIMEOUT, MAX_PDF_BYTES,
    )
    if status != 200:
        return status, (out if isinstance(out, str) else json.dumps(out)), err
    pdf_meta = out.get("metadata") or {}
    text = out.get("text") or ""
    if not text.strip():
        return 400, json.dumps({"error": "pdf contains no extractable text"}), None

    token = document.get("_token", "")          # access token, if the client sent one
    have_token = bool(document.get("_have_token"))
    cookie = document.get("_cookie", "")        # browser session cookie, if sent

    auth_headers = _client_auth_headers(token, cookie, have_token)
    csrf, opener = do_csrf_handshake(base_url, auth_headers)

    payload = {"url": url, "text": text}
    for f in DOC_FIELDS:
        v = document.get(f)
        if v:
            payload[f] = v
    if pdf_meta:
        # Prefer the PDF's embedded title over the client-supplied one: the
        # browser extension sends the current tab title, which for a PDF
        # navigated in a pre-existing tab is the PREVIOUS page's title (or an
        # interstitial like "Preparing to download ..."). Titles feed reranking,
        # so a stale/generic title actively hurts retrieval.
        if pdf_meta.get("title"):
            payload["title"] = pdf_meta["title"]
        payload["metadata"] = hister_metadata(pdf_meta)
    body = json.dumps(payload).encode("utf-8")
    headers = dict(auth_headers)
    headers.update(
        {
            "X-Csrf-Token": csrf,
            "Content-Type": "application/json",
            # CLI bypass: lets this downstream call skip the CSRF session check.
            "Origin": "hister://",
        }
    )
    req = urllib.request.Request(
        f"{base_url}/api/add",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with opener.open(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), None
    except Exception as e:  # noqa: BLE001
        return 502, json.dumps({"error": f"forward failed: {e}"}), str(e)


def handle_add_pdf(body: bytes, token: str, cookie: str, have_token: bool, fmt: str = "text") -> tuple:
    """Entry point: decode the /api/add_pdf payload and forward as /api/add."""
    try:
        req = json.loads(body or b"{}")
    except json.JSONDecodeError as e:
        return 400, json.dumps({"error": f"invalid JSON: {e}"}), None

    document = req.get("document") or {}
    pdf_b64 = req.get("pdf", "")
    if not document or not pdf_b64:
        return 400, json.dumps({"error": "missing document and/or pdf field"}), None
    if not document.get("url"):
        return 400, json.dumps({"error": "missing document.url"}), None

    try:
        pdf_bytes = base64.b64decode(pdf_b64, validate=True)
    except Exception as e:  # noqa: BLE001
        return 400, json.dumps({"error": f"pdf must be base64-encoded: {e}"}), None

    # Per-request extraction-format override (the extension's "pdf2md" button
    # sends document.format). Only text/markdown are valid; anything else falls
    # back to this handler's startup default (fmt).
    requested = document.get("format")
    if requested in ("text", "markdown"):
        fmt = requested
    # "format" is a proxy-only hint; never forward it to /api/add.
    document.pop("format", None)

    document["_token"] = token
    document["_cookie"] = cookie
    document["_have_token"] = have_token
    return forward_pdf(UPSTREAM, document, pdf_bytes, fmt)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "HisterPDFProxy/0.1"
    pdf_format = "text"  # overridden by main() from --format

    def _token(self) -> str:
        # The web-research provider sends `Authorization: Bearer ***`, but the
        # browser extension authenticates with `X-Access-Token` (network.ts).
        # Hister's requestAccessToken prefers X-Access-Token then falls back to
        # Bearer (server.go:194-198). Accept both so ANY /api/add_pdf client
        # (extension, provider, CLI) reaches the downstream /api/add authenticated.
        tok = self.headers.get("X-Access-Token", "")
        if not tok:
            auth = self.headers.get("Authorization", "")
            tok = auth[len("Bearer "):] if auth.lower().startswith("bearer ") else ""
        return tok

    def _log_body(self):
        if self.command == "POST" and self.headers.get("Content-Type", "").startswith("application/json"):
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""
        return b""

    def _send(self, status: int, body: bytes, ctype: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self, body: bytes):
        """Transparent forward of a non-PDF request to upstream, verbatim."""
        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in ("connection", "host", "content-length", "transfer-encoding")
        }
        url = f"{UPSTREAM}{self.path}"
        method = self.command.upper()
        req = urllib.request.Request(url, data=body or None, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                rbody = r.read()
                self.send_response(r.status)
                for k, v in r.headers.items():
                    if k.lower() not in ("connection", "transfer-encoding", "content-length"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(rbody)))
                self.end_headers()
                self.wfile.write(rbody)
        except urllib.error.HTTPError as e:
            rbody = e.read()
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() not in ("connection", "transfer-encoding", "content-length"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(rbody)))
            self.end_headers()
            self.wfile.write(rbody)
        except Exception as e:  # noqa: BLE001
            logger.warning("forward %s %s failed: %s", method, self.path, e)
            self._send(502, json.dumps({"error": f"proxy upstream failure: {e}"}).encode())

    def _route(self):
        if not UPSTREAM:
            self._send(500, json.dumps({"error": "HISTER_PROXY_UPSTREAM not set"}).encode())
            return
        body = self._log_body()
        if self.command.upper() == "POST" and self.path.rstrip("/") == "/api/add_pdf":
            token = self._token()
            cookie = self.headers.get("Cookie", "")
            status, resp, err = handle_add_pdf(body, token, cookie, have_token=bool(token),
                                               fmt=self.pdf_format)
            logger.info("add_pdf -> /api/add status=%s %s", status, "err=" + str(err) if err else "")
            self._send(status, resp.encode("utf-8"))
            return
        self._forward(body)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _route

    def log_message(self, fmt, *args):  # route through our logger
        logger.debug("%s - %s", self.address_string(), fmt % args)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--listen", default=os.getenv("HISTER_PROXY_LISTEN", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.getenv("HISTER_PROXY_PORT", "4434")))
    ap.add_argument(
        "--format",
        choices=("text", "markdown"),
        default=os.getenv("HISTER_PROXY_FORMAT", "text"),
        help="PDF extraction format: 'text' (fast, page.get_text) or "
        "'markdown' (pymupdf4llm layout, slower, structured tables/headings). "
        "markdown requires pymupdf4llm + pymupdf-layout in the venv.",
    )
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not UPSTREAM:
        logger.fatal("HISTER_PROXY_UPSTREAM must be set (e.g. http://host:4433)")
        sys.exit(2)

    ProxyHandler.pdf_format = a.format
    server = ThreadingHTTPServer((a.listen, a.port), ProxyHandler)
    logger.info("hister-pdf-proxy listening on %s:%d -> upstream %s (pdf format: %s)",
                a.listen, a.port, UPSTREAM, a.format)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
