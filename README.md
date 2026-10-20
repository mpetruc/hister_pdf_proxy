# hister-pdf-proxy

Reverse-proxy in front of a [Hister](https://github.com/asciimoo/hister) server
that repairs PDF ingestion.

## Problem

Hister's built-in PDF text extraction (`github.com/asciimoo/pdf` →
`GetPlainText()`) strips **all whitespace**, so PDFs index as unsearchable
one-line text. Because the mangling happens in Hister's own extractor, it
affects *every* ingestor that submits a PDF through the `/api/add_pdf`
endpoint — the browser extension, `hister import`, or any other HTTP client —
not just one client. Tracked upstream in
[asciimoo/hister#55](https://github.com/asciimoo/hister/issues/55).

## Approach

This proxy intercepts `POST /api/add_pdf`, decodes the base64 PDF, and extracts
clean, **whitespace-preserving** text locally — either fast flat text
(`page.get_text`, the default `--format text`) or structured markdown with real
table/heading detection (`--format markdown`, via pymupdf4llm's layout engine)
— then forwards the result to Hister via `/api/add` with the extracted text in
`document.text`, bypassing the broken PDF handler.

Every PDF also has its **embedded metadata extracted** (pymupdf `doc.metadata`)
and submitted in `document.metadata`: `author`, `description` (from the PDF
`subject` field), and `keywords` are surfaced top-level (Hister's embedding
context reads these — see `server/indexer/indexer.go`), while the remaining raw
keys (`title`, `format`, `creator`, `producer`, `creationDate`, `modDate`, …)
are preserved verbatim. Hister flattens the nested map to dotted keys
(`pdf.title`, `pdf.creator`, …), so nothing is lost.

**Title repair.** The browser extension sends the *current tab's* title, which
for a PDF loaded into a pre-existing tab is the **previous page's** title (or an
interstitial like "Preparing to download ..."). Titles feed reranking, so a
stale/generic title actively hurts retrieval. When the PDF has an embedded
`title`, the proxy uses **it** instead of the client-supplied one; if the PDF
has no embedded title, the client's title is kept. So PDFs get correct,
stable titles end-to-end.

Everything else (search, document reads, config, non-PDF traffic) is forwarded
unchanged.

> Why `/api/add` and not `hister import file`? `import` routes a `.pdf` through
> the **same** broken `pdfFileType` handler — `pdfFileType` is first in the
> handler list in `server/indexer/filetypes.go`. Only submitting already-
> extracted text via `/api/add` skips it.

## Usage

`hister-pdf-proxy` is a uv package. The core dependency is `pymupdf`; the
`markdown` extra pulls `pymupdf4llm` + `pymupdf-layout` for structured output.

```bash
# run directly from the repo (dev)
uv sync            # installs core deps; add --extra markdown for --format markdown
uv run hister-pdf-proxy --help

# run the console script (upstream comes from HISTER_PROXY_UPSTREAM env)
export HISTER_PROXY_UPSTREAM=http://192.168.3.120:4433
uv run hister-pdf-proxy --listen 0.0.0.0 --port 4434
```

Binary distribution (wheel/sdist):
```bash
uv build                          # dist/hister_pdf_proxy-*.whl / .tar.gz
# install into a venv (optionally with the markdown extra)
uv venv .venv --python /usr/bin/python3.13
uv pip install --python .venv/bin/python ".[markdown]"
.venv/bin/hister-pdf-proxy --listen 0.0.0.0 --port 4434
```

Production (systemd) install is handled by `install/setup.sh`, which installs
this same package into `/opt/hister-pdf-proxy/.venv`. See the "Deployment"
section.

Then point Hister clients at the proxy instead of Hister's port:

```bash
# our web-research provider
export WEBRESEARCH_HISTER_URL=http://<proxy-host>:4434
# browser extension: repoint the Hister endpoint to the proxy
```

## Deployment (systemd)

```bash
sudo bash install/setup.sh            # full install (idempotent)
sudo bash install/setup.sh --sync     # reinstall package + restart (after code changes)
```

Installs into `/opt/hister-pdf-proxy`, creates a dedicated `histerp` user, builds
the venv on `/usr/bin/python3.13`, and installs this package **with the
`markdown` extra**. Runs as `hister-pdf-proxy.service` on `0.0.0.0:4434`,
hardened (`ProtectSystem=strict`, `ProtectHome=true`, …; `MemoryDenyWriteExecute`
deliberately omitted — PyMuPDF/libmupdf needs W+X trampolines).

Startup config lives in `/etc/hister-pdf-proxy/env`:

```ini
HISTER_PROXY_UPSTREAM=http://192.168.3.120:4433
#HISTER_PROXY_FORMAT=markdown     # uncomment for structured markdown
```

## Behavior

- **`POST /api/add_pdf`** → local PyMuPDF extraction → `/api/add` with clean
  `document.text` **plus** `document.metadata` (author/description/keywords +
  raw PDF keys under `pdf.*`). Returns 201/406/422 to the client on success.
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
| `HISTER_PROXY_FORMAT` | `--format` | `text` | PDF extraction: `text` (fast, `page.get_text`) or `markdown` (pymupdf4llm layout, slower, structured tables/headings) |
| `HISTER_PROXY_EXTRACT_TIMEOUT` | — | `120` | Hard wall-clock budget (seconds) for one PDF extraction. Runs in a subprocess so a timeout actually kills the CPU work — a huge/pathological PDF fails with 504 instead of silently grinding for minutes. |
| `HISTER_PROXY_MAX_BYTES` | — | `0` (unlimited) | Reject PDFs larger than this (bytes) with 413 before extraction. `0` disables the cap. |

The `--format` flag is chosen at startup: either `text` (default, no `pymupdf4llm`
needed, fast) or `markdown` (layout engine + `pymupdf4llm`, slower, real table
detection + heading hierarchy). The systemd unit's `ExecStart` can pass
`--format markdown` (or set `HISTER_PROXY_FORMAT` in `/etc/hister-pdf-proxy/env`).

## Notes / limitations

- Does **not** store the original PDF as a file — Hister's `Document` has no
  pdf/blob key for network-submitted content. If retaining the original PDF is
  required, that's a separate Hister feature (or a side-channel store).
- TLS-termined HTTPS in front of the proxy is the caller's concern; it speaks
  plain HTTP to both sides.
- If `asciimoo/hister#55` is fixed upstream and the built-in extractor becomes
  whitespace-correct, this proxy becomes unnecessary for PDFs; it can be retired
  by repointing clients back at Hister.
