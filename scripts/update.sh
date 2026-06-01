#!/usr/bin/env bash
# update.sh — ritual de self-update pós feature merge (ADR-009).
# Combina `git pull --ff-only` (falha-fast em diverge) com restart do daemon
# (Python sem hot-reload — restart necessário para reimportar módulo).
# CLI reflete imediatamente (pipx em modo editable aponta pro checkout).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\e[1;34m==>\e[0m %s\n' "$*"; }

say "Atualizando checkout em $PROJECT_DIR..."
git -C "$PROJECT_DIR" pull --ff-only

say "Reiniciando drive-sync.service..."
systemctl --user restart drive-sync.service

printf '\n\e[1;32m✓\e[0m drive-sync atualizado e reiniciado.\n'
