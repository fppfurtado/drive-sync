#!/usr/bin/env bash
# install.sh — instalação para Fedora 43 (single-user)
# Uso: bash install.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}/drive-sync"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

say()  { printf '\e[1;34m==>\e[0m %s\n' "$*"; }
warn() { printf '\e[1;33m[!] %s\e[0m\n' "$*"; }

# 1) Dependências de sistema (Fedora 43)
say "Instalando dependências de sistema (rclone, git, python3, pipx)..."
sudo dnf install -y rclone git python3 python3-pip pipx fuse-overlayfs >/dev/null

# 2) Garante pipx no PATH (Fedora coloca em ~/.local/bin)
pipx ensurepath >/dev/null

# 3) Instala o pacote no user-site via pipx
say "Instalando o pacote drive-sync..."
pipx install --force "$PROJECT_DIR"

# 4) Cria o config se não existir
mkdir -p "$CONFIG_HOME"
if [[ ! -f "$CONFIG_HOME/config.yaml" ]]; then
  cp "$PROJECT_DIR/config/config.example.yaml" "$CONFIG_HOME/config.yaml"
  say "Config inicial criado em: $CONFIG_HOME/config.yaml"
  warn "Edite-o agora para listar suas pastas — depois rode 'rclone config' para criar o remote 'drive'."
else
  say "Config já existe em $CONFIG_HOME/config.yaml — preservado."
fi

# 5) Instala a unit do systemd --user
say "Instalando unit systemd --user..."
mkdir -p "$SYSTEMD_USER_DIR"
cp "$PROJECT_DIR/systemd/drive-sync.service" "$SYSTEMD_USER_DIR/"
systemctl --user daemon-reload

# 6) Habilita lingering para o serviço subir antes do login (boot do SO)
if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
  say "Habilitando lingering para que o serviço suba com o boot..."
  sudo loginctl enable-linger "$USER"
fi

# 7) Habilita o serviço (sem iniciar — pede para o usuário configurar primeiro)
systemctl --user enable drive-sync.service >/dev/null

cat <<EOF

✅ Instalação concluída.

Próximos passos:
  1) Configure o remote rclone (uma vez só):
       rclone config           # escolha 'New remote', nome 'drive', tipo 'Proton Drive'
  2) Edite suas pastas em:
       $CONFIG_HOME/config.yaml
  3) Valide:
       drive-sync --check
  4) Inicie o serviço:
       systemctl --user start drive-sync
  5) Acompanhe os logs:
       journalctl --user -u drive-sync -f
       # ou:
       tail -f ~/.local/state/drive-sync/drive-sync.log
EOF
