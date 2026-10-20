#!/usr/bin/env bash
# Install or update hister-pdf-proxy as a hardened systemd service.
#   sudo bash setup.sh          # full install (idempotent; safe to rerun)
#   sudo bash setup.sh --sync   # fast deploy: reinstall package + restart
# Run as root.
set -euo pipefail

APP_DIR=/opt/hister-pdf-proxy
SVC=hister-pdf-proxy
USER=histerp
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"          # this dir (unit, env)
SRC_DIR="$(cd "$INSTALL_DIR/.." && pwd)"                            # repo root (src/, pyproject.toml)

# uv must be on root's PATH (e.g. /usr/local/bin)
command -v uv >/dev/null 2>&1 || { echo "!! 'uv' not found on PATH (checked by this script as root)." >&2; exit 1; }

_pkg_install () {  # install THIS package (with markdown extra) into APP_DIR venv
  uv pip install --python "$APP_DIR/.venv/bin/python" \
    --no-cache --reinstall-package hister-pdf-proxy \
    "$SRC_DIR[markdown]"
}

# --- --sync: reinstall code, restart -------------------------------------------------
if [ "${1:-}" = "--sync" ]; then
  echo "==> sync mode"
  if [ ! -d "$APP_DIR/.venv" ]; then
    echo "!! $APP_DIR/.venv missing - run 'sudo bash setup.sh' (full install) first" >&2
    exit 1
  fi
  _pkg_install
  # Re-apply runtime ownership + exec bits. uv pip install runs as root and
  # recreates the console script, which can lose the service user's execute
  # access (Errno 13 Permission denied on start) unless we re-chown/chmod —
  # the full-install path does this, but --sync previously skipped it.
  chown -R root:"$USER" "$APP_DIR/.venv"
  chmod -R o-rwx "$APP_DIR/.venv"
  chmod 0755 "$APP_DIR/.venv/bin/hister-pdf-proxy"
  chmod 0755 "$APP_DIR/.venv/bin/python"
  echo "==> reinstalled hister-pdf-proxy into $APP_DIR/.venv"
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

echo "==> installing package sources to ${APP_DIR}"
install -d -o root -g root -m 0755 "$APP_DIR"
install -o root -g root -m 0644 "$SRC_DIR/pyproject.toml" "$APP_DIR/pyproject.toml"
rm -rf "$APP_DIR/src"
cp -r "$SRC_DIR/src" "$APP_DIR/src"          # hister_pdf_proxy package
chown -R root:root "$APP_DIR/src"
chmod -R o-rwx "$APP_DIR/src"
echo "    pyproject.toml + src/ -> $APP_DIR"

echo "==> venv + package install"
if [ -x "$APP_DIR/.venv/bin/hister-pdf-proxy" ]; then
  echo "    venv + console script present -> reinstalling package"
else
  echo "    creating venv (BASE ON SYSTEM PYTHON, not uv cache:"
  echo "    a uv-managed python symlinks into ~/.local/share/uv/python which the unit"
  echo "    rejects under ProtectHome=true -> status 203/EXEC)"
  uv venv "$APP_DIR/.venv" --python /usr/bin/python3.13
fi
_pkg_install

# venv + app readable/executable by the runtime user (root:histerp), not others
chown -R root:"$USER" "$APP_DIR/.venv"
chown -R root:root "$APP_DIR/src"
chmod -R o-rwx "$APP_DIR/.venv"
chmod 0755 "$APP_DIR/.venv/bin/hister-pdf-proxy"
chmod 0755 "$APP_DIR/.venv/bin/python"

echo "==> installing envfile + unit"
install -d -o root -g "$USER" -m 0750 /etc/hister-pdf-proxy
install -o root -g "$USER" -m 0640 "$INSTALL_DIR/env" /etc/hister-pdf-proxy/env
install -o root -g root  -m 0644 "$INSTALL_DIR/hister-pdf-proxy.service" /etc/systemd/system/hister-pdf-proxy.service

echo "==> reload + enable + start"
systemctl daemon-reload
systemctl enable "$SVC"
# restart (not just enable --now): enable --now does NOT restart an already-
# running service, so a stale process would keep serving old code after install.
systemctl restart "$SVC"

echo
echo "==> status check"
systemctl --no-pager --full status "$SVC" | head -20 || true
echo
echo "==> listening socket"
ss -ltnp | grep ':4434' || echo "4434 not listening!"
echo "==> done"
