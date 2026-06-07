#!/usr/bin/env bash
# Install the orchestrator as a self-restarting systemd --user service.
# Idempotent: safe to re-run after a git pull.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> venv + deps"
if command -v uv >/dev/null 2>&1; then
  [ -d .venv ] || uv venv .venv -q
  uv pip install -q --python .venv/bin/python -e '.[dev]'
else
  [ -d .venv ] || python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -e '.[dev]'
fi

mkdir -p state

echo "==> systemd --user unit"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
cp systemd/harness-google-sheet.service "$UNIT_DIR/"
systemctl --user daemon-reload

echo "==> linger (survive logout / reboot)"
loginctl enable-linger "$USER" || echo "  (could not enable linger; ask an admin if needed)"

echo
echo "Done. Next:"
echo "  1. Create .env (see README) with SHEET_ID + GOOGLE_SA_JSON."
echo "  2. Validate:   ./.venv/bin/python -m sheet_agent doctor"
echo "  3. Start:      systemctl --user enable --now harness-google-sheet"
echo "  4. Logs:       journalctl --user -u harness-google-sheet -f"
echo
echo "Once running, the daemon self-seeds the _skills catalog tab — the"
echo "'▶️ Запустить скилл' menu works as soon as the catalog appears."
echo "Deploy the Apps Script menu separately (manual OAuth): cd appsscript && clasp push."
