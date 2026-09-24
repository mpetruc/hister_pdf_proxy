# hister-pdf-proxy

Reverse-proxy in front of a [Hister](https://github.com/asciimoo/hister) server
that repairs PDF ingestion.

## Problem

Hister's built-in PDF text extraction (`github.com/asciimoo/pdf` →
`GetPlainText()`) strips **all whitespace**, so PDFs index as unsearchable
one-line text. This affects every ingestor, not just one client: our
`web-research-extract` *and* the browser extension both send the same
`/api/add_pdf` shape and both get mangled text. Tracked upstream in
[asciimoo/hister#55](https://github.com/asciimoo/hister/issues/55).

## Approach

This proxy intercepts `POST /api/add_pdf`, decodes the base64 PDF, extracts
clean, **whitespace-preserving** text locally with PyMuPDF, and forwards the
result to Hister via `/api/add` with the extracted text in `document.text` —
bypassing the broken PDF handler.

Everything else (search, document reads, config, non-PDF traffic) is forwarded
unchanged.

> Why `/api/add` and not `hister import file`? `import` routes a `.pdf` through
> the **same** broken `pdfFileType` handler — `pdfFileType` is first in the
> handler list in `server/indexer/filetypes.go`. Only submitting already-
> extracted text via `/api/add` skips it.

## Usage

```bash
# venv (uv)
uv venv && uv pip install pymupdf

# run
export HISTER_PROXY_UPSTREAM=http://192.168.3.120:4433
./.venv/bin/python proxy.py --listen 0.0.0.0 --port 4434
```

Then point Hister clients at the proxy instead of Hister's port:

```bash
# our web-research provider
export WEBRESEARCH_HISTER_URL=http://<proxy-host>:4434
# browser extension: repoint the Hister endpoint to the proxy
```

## Behavior

- **`POST /api/add_pdf`** → local PyMuPDF extraction → `/api/add` with clean
  `document.text`. Returns 201/406/422 to the client on success.
- **Everything else** → transparent byte-for-byte forward to upstream
  (`Authorization` header, CSRF session, cookies, query string all preserved;
  only `Connection`/`Host`/`Content-Length`/`Transfer-Encoding` are re-derived).
- **Auth**: the client's `Authorization: Bearer <token>` is forwarded unchanged.
  The downstream `/api/add` call reuses it and bypasses the CSRF session check
  via the `Origin: hister://` header Hister reserves for CLI-originated
  requests — the client's own session/CSRF flow is untouched.

## Env / args

| Env | Arg | Default | Purpose |
|---|---|---|---|
| `HISTER_PROXY_UPSTREAM` | — | *(required)* | Upstream Hister base URL |
| `HISTER_PROXY_LISTEN` | `--listen` | `0.0.0.0` | Bind address |
| `HISTER_PROXY_PORT` | `--port` | `4434` | Listen port |

## Notes / limitations

- Does **not** store the original PDF as a file — Hister's `Document` has no
  pdf/blob key for network-submitted content. If retaining the original PDF is
  required, that's a separate Hister feature (or a side-channel store).
- TLS-termined HTTPS in front of the proxy is the caller's concern; it speaks
  plain HTTP to both sides.
- If `asciimoo/hister#55` is fixed upstream and the built-in extractor becomes
  whitespace-correct, this proxy becomes unnecessary for PDFs; it can be retired
  by repointing clients back at Hister.
