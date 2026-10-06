#!/usr/bin/env bash
# update.sh — ritual de self-update pós feature merge (ADR-009).
# Combina `git pull --ff-only` (falha-fast em diverge) com restart do daemon
# (Python sem hot-reload — restart necessário para reimportar módulo).
# CLI reflete imediatamente (pipx em modo editable aponta pro checkout).
# As units systemd são cópias (não symlinks): re-instala as que mudaram
# antes do restart, senão a mudança de unit nunca chega ao host (#102).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

say() { printf '\e[1;34m==>\e[0m %s\n' "$*"; }

say "Atualizando checkout em $PROJECT_DIR..."
git -C "$PROJECT_DIR" pull --ff-only

# shellcheck source=scripts/lib-units.sh
source "$PROJECT_DIR/scripts/lib-units.sh"
sync_units "$PROJECT_DIR" "$SYSTEMD_USER_DIR"
if ((${#UNITS_CHANGED[@]} > 0)); then
  say "Units re-instaladas (daemon-reload feito): ${UNITS_CHANGED[*]}"
  # Mudança de timer só vale por inteiro com restart do próprio timer.
  if [[ " ${UNITS_CHANGED[*]} " == *" drive-sync-watchdog.timer "* ]]; then
    systemctl --user restart drive-sync-watchdog.timer
  fi
fi

say "Reiniciando drive-sync.service..."
systemctl --user restart drive-sync.service

printf '\n\e[1;32m✓\e[0m drive-sync atualizado e reiniciado.\n'
