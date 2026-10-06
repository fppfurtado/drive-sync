# shellcheck shell=bash
# lib-units.sh — instalação das units systemd --user, compartilhada entre
# install.sh e update.sh (#102). Sourced, não executado.
#
# As units são COPIADAS para o diretório do systemd (não symlinkadas), então uma
# mudança em systemd/* só chega ao host se alguém copiar de novo. sync_units
# copia as que diferem e faz daemon-reload quando algo mudou — sem isso o
# update.sh deixava toda mudança de unit presa no repo, sem aviso.

UNITS=(drive-sync.service drive-sync-watchdog.service drive-sync-watchdog.timer)

# sync_units <project_dir> <systemd_user_dir>
# Copia cada unit de UNITS que estiver ausente ou divergente e roda
# `systemctl --user daemon-reload` se pelo menos uma mudou. Deixa em
# UNITS_CHANGED os nomes copiados (vazio = nada mudou).
sync_units() {
  local project_dir="$1" dest="$2" unit
  UNITS_CHANGED=()
  mkdir -p "$dest"
  for unit in "${UNITS[@]}"; do
    if ! cmp -s "$project_dir/systemd/$unit" "$dest/$unit"; then
      cp "$project_dir/systemd/$unit" "$dest/$unit"
      UNITS_CHANGED+=("$unit")
    fi
  done
  if ((${#UNITS_CHANGED[@]} > 0)); then
    systemctl --user daemon-reload
  fi
}
