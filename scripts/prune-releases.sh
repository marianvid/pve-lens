#!/usr/bin/env bash
set -euo pipefail

release_root=/opt/pve-lens/releases
current_link=/opt/pve-lens/current
keep_inactive=1

current_release=$(readlink -f "$current_link")
mapfile -t releases < <(find "$release_root" -mindepth 1 -maxdepth 1 -type d -name '20??????-???' -print | sort -r)

inactive_kept=0
for release in "${releases[@]}"; do
  resolved=$(readlink -f "$release")
  [[ "$resolved" == "$current_release" ]] && continue

  if (( inactive_kept < keep_inactive )); then
    ((inactive_kept += 1))
    continue
  fi

  [[ "$resolved" == "$release_root"/* ]] || {
    echo "Refusing to remove path outside $release_root: $resolved" >&2
    exit 1
  }
  rm -rf -- "$resolved"
done
