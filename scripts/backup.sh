#!/usr/bin/env bash
# Consistent offline application snapshot; PostgreSQL remains online for pg_dump.
set -euo pipefail
umask 077

if [[ $# != 2 ]]; then
  echo "Usage: $0 RUNTIME_DIRECTORY NEW_BACKUP.tar.gz" >&2
  exit 2
fi
integration_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
runtime=$(realpath -e -- "$1")
archive=$(realpath -m -- "$2")
[[ -d "$runtime" && -f "$runtime/compose.env" ]] || { echo 'Runtime compose.env is required.' >&2; exit 1; }
[[ ! -e "$archive" && ! -L "$archive" ]] || { echo 'Backup destination already exists.' >&2; exit 1; }
[[ "$archive" != "$runtime" && "$archive" != "$runtime/"* ]] || { echo 'Backup must be outside the runtime directory.' >&2; exit 1; }
[[ -d "$(dirname -- "$archive")" ]] || { echo 'Create the backup parent directory first.' >&2; exit 1; }
selection=$(python3 "$integration_root/scripts/recovery.py" inspect "$runtime" "$integration_root")
mapfile -t deployment <<< "$selection"
mode=${deployment[0]}
read -r -a selected_services <<< "${deployment[2]}"
command -v docker >/dev/null || { echo 'Docker Compose is required.' >&2; exit 1; }
command -v flock >/dev/null || { echo 'flock is required.' >&2; exit 1; }
exec 9>"$runtime/.maintenance.lock"
flock -n 9 || { echo 'Another backup or maintenance task holds the runtime lock.' >&2; exit 1; }
compose=(docker compose --env-file "$runtime/compose.env" --file "${deployment[1]}" --profile '*')
staging=$(mktemp -d "${TMPDIR:-/tmp}/tailnet-backup.XXXXXX")
partial=$(mktemp "$archive.partial.XXXXXX")
restart_services=()
restart_required=false
cleanup() {
  status=$?
  trap - EXIT INT TERM
  if [[ "$restart_required" == true && ${#restart_services[@]} -gt 0 ]]; then
    if ! "${compose[@]}" start "${restart_services[@]}"; then
      echo 'Some original services could not restart; inspect the deployment immediately.' >&2
      status=1
    fi
  fi
  rm -rf -- "$staging"
  rm -f -- "$partial"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Stop only owned writers; remember exactly which ones were running.
writers=()
for service in worker headplane headscale casdoor proxy; do
  for selected in "${selected_services[@]}"; do
    [[ "$selected" != "$service" ]] || writers+=("$service")
  done
done
running=$("${compose[@]}" ps --services --status running)
while IFS= read -r service; do
  for writer in "${writers[@]}"; do
    [[ "$service" != "$writer" ]] || restart_services+=("$service")
  done
done <<< "$running"
restart_required=true
"${compose[@]}" stop --timeout 60 "${writers[@]}"
if [[ "$mode" == bundled ]]; then
  "${compose[@]}" exec -T db pg_dump --username casdoor --dbname casdoor --format custom > "$staging/database.dump"
  [[ -s "$staging/database.dump" ]] || { echo 'Database dump is empty.' >&2; exit 1; }
fi
python3 "$integration_root/scripts/recovery.py" stage "$runtime" "$staging" "$integration_root"
proxy_container=$("${compose[@]}" ps --all --quiet proxy)
if [[ -n "$proxy_container" ]]; then
  "${compose[@]}" cp proxy:/data/. "$staging/caddy_data/"
  "${compose[@]}" cp proxy:/config/. "$staging/caddy_config/"
fi
tar -C "$staging" -czf "$partial" .
chmod 600 "$partial"
# Atomic no-overwrite publication, even if another process creates the destination.
ln -- "$partial" "$archive"
echo "Backup written to $archive (mode 600; contains credentials and private keys)."
sha256sum -- "$archive"
