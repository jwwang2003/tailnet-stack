#!/usr/bin/env bash
# Restore into a new directory and a new Compose project; never start app writers.
set -euo pipefail
umask 077

if [[ $# != 4 && $# != 6 ]]; then
  echo "Usage: $0 BACKUP.tar.gz EXPECTED_versions.lock.yaml NEW_DIRECTORY NEW_COMPOSE_PROJECT [--expected-mode bundled|external]" >&2
  exit 2
fi
integration_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
expected_mode=''
if [[ $# == 6 ]]; then
  [[ "$5" == --expected-mode && ( "$6" == bundled || "$6" == external ) ]] || { echo 'Use --expected-mode bundled|external.' >&2; exit 2; }
  expected_mode=$6
fi
archive=$(realpath -e -- "$1")
expected_lock=$(realpath -e -- "$2")
destination=$(realpath -m -- "$3")
project=$4
[[ "$project" =~ ^[a-z0-9][a-z0-9_-]{2,50}$ ]] || { echo 'Use a new simple lowercase Compose project name (3-51 characters).' >&2; exit 1; }
[[ ! -e "$destination" && ! -L "$destination" ]] || { echo 'Restore directory must not exist; nothing was overwritten.' >&2; exit 1; }
[[ "$destination" != *[[:space:]\$\#]* ]] || { echo 'Restore path cannot contain whitespace, dollar signs, or #.' >&2; exit 1; }
[[ -d "$(dirname -- "$destination")" ]] || { echo 'Create the restore parent directory first.' >&2; exit 1; }
# Validate the complete archive before filesystem or container changes.
selection=$(python3 "$integration_root/scripts/recovery.py" verify "$archive" "$expected_lock" "$destination" "$project" "$expected_mode")
mapfile -t deployment <<< "$selection"
mode=${deployment[0]}
command -v docker >/dev/null || { echo 'Docker Compose is required.' >&2; exit 1; }
existing=$(docker ps --all --quiet --filter "label=com.docker.compose.project=$project")
[[ -z "$existing" ]] || { echo 'Compose project already has containers; use a fresh project.' >&2; exit 1; }
volumes=(caddy_data caddy_config)
[[ "$mode" != bundled ]] || volumes+=(casdoor_db)
for volume in "${volumes[@]}"; do
  if docker volume inspect "${project}_$volume" >/dev/null 2>&1; then
    echo "Volume ${project}_$volume already exists; use a fresh project." >&2
    exit 1
  fi
done
python3 "$integration_root/scripts/recovery.py" extract "$archive" "$expected_lock" "$destination" "$project" "$expected_mode" >/dev/null
runtime="$destination/runtime"
compose=(docker compose --project-name "$project" --env-file "$runtime/compose.env" --file "${deployment[1]}" --profile '*')
"${compose[@]}" config --quiet
if [[ "$mode" == bundled ]]; then
  "${compose[@]}" up --detach db
  ready=false
  for attempt in {1..60}; do
    if "${compose[@]}" exec -T db pg_isready --username casdoor --dbname casdoor >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 1
  done
  [[ "$ready" == true ]] || { echo 'New database did not become ready; restored files remain for inspection.' >&2; exit 1; }
  "${compose[@]}" exec -T db pg_restore --username casdoor --dbname casdoor --exit-on-error --no-owner --no-privileges < "$destination/database.dump"
fi
"${compose[@]}" create proxy
"${compose[@]}" cp "$destination/caddy_data/." proxy:/data/
"${compose[@]}" cp "$destination/caddy_config/." proxy:/config/
echo "Restored into $destination with isolated Compose project $project."
if [[ "$mode" == bundled ]]; then
  echo 'Only the new database is running. Inspect source/image pins and keep DNS/public ports isolated before starting applications.'
else
  echo 'No application or worker is running. The external identity service was not accessed; verify its recovery separately.'
fi
echo 'Use this restore project name on every subsequent Compose command; do not omit --project-name.'
