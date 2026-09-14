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
python3 - "$runtime" <<'PY'
import pathlib, stat, sys
runtime = pathlib.Path(sys.argv[1])
values = dict(line.split('=', 1) for line in (runtime / 'compose.env').read_text().splitlines() if '=' in line)
if values.get('RUNTIME_DIR') != str(runtime):
    raise SystemExit('compose.env RUNTIME_DIR differs from the requested backup source')
for path in runtime.rglob('*'):
    if path.relative_to(runtime).as_posix() == 'headscale/data/headscale.sock':
        continue
    mode = path.lstat().st_mode
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise SystemExit('Runtime contains a link or special file that cannot be safely restored: ' + str(path))
PY
command -v docker >/dev/null || { echo 'Docker Compose is required.' >&2; exit 1; }
command -v flock >/dev/null || { echo 'flock is required.' >&2; exit 1; }
exec 9>"$runtime/.maintenance.lock"
flock -n 9 || { echo 'Another backup or maintenance task holds the runtime lock.' >&2; exit 1; }
compose=(docker compose --env-file "$runtime/compose.env" --file "$integration_root/deploy/compose.yaml" --profile '*')
staging=$(mktemp -d "${TMPDIR:-/tmp}/feishu-backup.XXXXXX")
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

running=$("${compose[@]}" ps --services --status running)
while IFS= read -r service; do
  case "$service" in worker|headplane|headscale|casdoor|proxy) restart_services+=("$service");; esac
done <<< "$running"
restart_required=true
"${compose[@]}" stop --timeout 60 worker headplane headscale casdoor proxy
"${compose[@]}" exec -T db pg_dump --username casdoor --dbname casdoor --format custom > "$staging/database.dump"
[[ -s "$staging/database.dump" ]] || { echo 'Database dump is empty.' >&2; exit 1; }
mkdir "$staging/runtime" "$staging/deploy" "$staging/caddy_data" "$staging/caddy_config"
# A stopped Headscale Unix socket is not persistent state; tar cannot restore it.
tar --exclude='./headscale/data/headscale.sock' --exclude='./.maintenance.lock' -C "$runtime" -cf - . | tar -C "$staging/runtime" -xf -
cp -- "$integration_root/deploy/compose.yaml" "$integration_root/deploy/Caddyfile" "$staging/deploy/"
cp -- "$integration_root/versions.lock.yaml" "$staging/versions.lock.yaml"
proxy_container=$("${compose[@]}" ps --all --quiet proxy)
if [[ -n "$proxy_container" ]]; then
  "${compose[@]}" cp proxy:/data/. "$staging/caddy_data/"
  "${compose[@]}" cp proxy:/config/. "$staging/caddy_config/"
fi
python3 - "$staging" "$integration_root" <<'PY'
import hashlib, json, pathlib, subprocess, sys
stage, source = map(pathlib.Path, sys.argv[1:])
from datetime import datetime, timezone
metadata = {
    'schema_version': 1,
    'created_at_utc': datetime.now(timezone.utc).isoformat(),
    'integration_commit': subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
    'versions_lock_sha256': hashlib.sha256((stage / 'versions.lock.yaml').read_bytes()).hexdigest(),
    'database_dump_sha256': hashlib.sha256((stage / 'database.dump').read_bytes()).hexdigest(),
}
(stage / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
PY
tar -C "$staging" -czf "$partial" .
chmod 600 "$partial"
# Atomic no-overwrite publication, even if another process creates the destination.
ln -- "$partial" "$archive"
echo "Backup written to $archive (mode 600; contains credentials and private keys)."
sha256sum -- "$archive"
