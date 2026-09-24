#!/usr/bin/env bash
# Install or update hister-pdf-proxy as a hardened systemd service.
#   sudo bash setup.sh          # full install (idempotent; safe to rerun)
#   sudo bash setup.sh --sync   # fast deploy: just sync proxy.py + restart
# Run as root.
set -euo pipefail

APP_DIR=/opt/hister-pdf-proxy
SVC=hister-pdf-proxy
USER=histerp
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"          # this dir (unit, env)
SRC_DIR="$(cd "$INSTALL_DIR/.." && pwd)"                            # repo root (proxy.py, .venv)

# --- --sync: latest code only, no user/venv/unit/enable changes -----------------
if [ "${1:-}" = "--sync" ]; then
  echo "==> sync mode"
  if [ ! -d "$APP_DIR" ]; then
    echo "!! $APP_DIR missing - run 'sudo bash setup.sh' (full install) first" >&2
    exit 1
  fi
  install -o root -g root -m 0755 "$SRC_DIR/proxy.py" "$APP_DIR/proxy.py"
  echo "    proxy.py synced -> $APP_DIR/proxy.py"
  systemctl restart "$SVC"
  echo "==> restarted $SVC"
  systemctl --no-pager --full status "$SVC" | head -8 || true
  ss -ltnp | grep ':4434' >/dev/null && echo "==> 4434 listening" || echo "!! 4434 not listening" >&2
  exit 0
fi

echo "==> creating dedicated system user '${USER}'"
if ! id -u "$USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$USER"
else
  echo "    (already exists)"
fi

echo "==> installing app to ${APP_DIR}"
install -d -o "$USER" -g "$USER" -m 0755 "$APP_DIR"
install -o root -g root -m 0755 "$SRC_DIR/proxy.py" "$APP_DIR/proxy.py"
echo "    proxy.py synced -> $APP_DIR/proxy.py"

echo "==> venv + pymupdf"
if [ -x "$APP_DIR/.venv/bin/python" ] && "$APP_DIR/.venv/bin/python" -c "import pymupdf" >/dev/null 2>&1; then
  echo "    venv already present with pymupdf -> skipping rebuild"
  echo "    (to rebuild: rm -rf $APP_DIR/.venv && rerun)"
else
  echo "    creating venv + installing pymupdf (BASE ON SYSTEM PYTHON, not uv cache:"
  echo "    a uv-managed python symlinks into ~/.local/share/uv/python which the unit"
  echo "    rejects under ProtectHome=true -> status 203/EXEC)"
  uv venv "$APP_DIR/.venv" --python /usr/bin/python3.13
  uv pip install --python "$APP_DIR/.venv/bin/python" pymupdf
fi
# venv + app readable/executable by the runtime user (root:histerp), not others
chown -R root:"$USER" "$APP_DIR"
chmod -R o-rwx "$APP_DIR"
find "$APP_DIR" -type d -exec chmod 0750 {} +
find "$APP_DIR" -type f -exec chmod 0640 {} +
chmod 0755 "$APP_DIR/.venv/bin/python"

echo "==> installing envfile + unit"
install -d -o root -g "$USER" -m 0750 /etc/hister-pdf-proxy
install -o root -g "$USER" -m 0640 "$INSTALL_DIR/env" /etc/hister-pdf-proxy/env
install -o root -g root  -m 0644 "$INSTALL_DIR/hister-pdf-proxy.service" /etc/systemd/system/hister-pdf-proxy.service

echo "==> reload + enable + start"
systemctl daemon-reload
systemctl enable --now "$SVC"

echo
echo "==> status check"
systemctl --no-pager --full status "$SVC" | head -20 || true
echo
echo "==> listening socket"
ss -ltnp | grep ':4434' || echo "(4434 not listening!)"
echo "==> done"
