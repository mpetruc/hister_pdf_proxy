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


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Local, whitespace-preserving PDF text extraction (PyMuPDF)."""
    import fitz  # pymupdf, installed in this venv
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    parts = []
    for page in doc:
        parts.append(page.get_text())
    doc.close()
    return "\n".join(parts)


def do_csrf_handshake(base_url: str, token: str):
    """Return (csrf token, cookie jar) for a downstream /api/add POST.

    Mirrors HisterClient.add(): GET /api/config through the same CookieJar to
    bind the X-Csrf-Token to the session cookie.
    """
    import http.cookiejar as cookiejar

    jar = cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(
        f"{base_url}/api/config",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    with opener.open(req, timeout=20) as r:
        csrf = r.headers.get("X-Csrf-Token", "")
    return csrf, opener


def forward_pdf(base_url: str, document: dict, pdf_bytes: bytes) -> tuple:
    """Extract PDF text locally and POST to /api/add.

    Returns (status_code, response_body, error_string).
    """
    text = extract_pdf_text(pdf_bytes)
    if not text.strip():
        return 400, json.dumps({"error": "pdf contains no extractable text"}), None

    url = document.get("url", "")
    token = document.get("_token", "")  # injected by handler
    csrf, opener = do_csrf_handshake(base_url, token)

    payload = {"url": url, "text": text}
    for f in DOC_FIELDS:
        v = document.get(f)
        if v:
            payload[f] = v
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/add",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Csrf-Token": csrf,
            "Content-Type": "application/json",
            # CLI bypass: lets this downstream call skip the CSRF session check.
            "Origin": "hister://",
        },
        method="POST",
    )
    try:
        with opener.open(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), None
    except Exception as e:  # noqa: BLE001
        return 502, json.dumps({"error": f"forward failed: {e}"}), str(e)


def handle_add_pdf(body: bytes, token: str) -> tuple:
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

    document["_token"] = token
    return forward_pdf(UPSTREAM, document, pdf_bytes)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "HisterPDFProxy/0.1"

    def _token(self) -> str:
        auth = self.headers.get("Authorization", "")
        return auth[len("Bearer "):] if auth.lower().startswith("bearer ") else ""

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
            status, resp, err = handle_add_pdf(body, self._token())
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
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not UPSTREAM:
        logger.fatal("HISTER_PROXY_UPSTREAM must be set (e.g. http://host:4433)")
        sys.exit(2)

    server = ThreadingHTTPServer((a.listen, a.port), ProxyHandler)
    logger.info("hister-pdf-proxy listening on %s:%d -> upstream %s",
                a.listen, a.port, UPSTREAM)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
