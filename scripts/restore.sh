#!/usr/bin/env bash
# Restore into a new directory and a new Compose project; never start app writers.
set -euo pipefail
umask 077

if [[ $# != 4 ]]; then
  echo "Usage: $0 BACKUP.tar.gz EXPECTED_versions.lock.yaml NEW_DIRECTORY NEW_COMPOSE_PROJECT" >&2
  exit 2
fi
archive=$(realpath -e -- "$1")
expected_lock=$(realpath -e -- "$2")
destination=$(realpath -m -- "$3")
project=$4
[[ "$project" =~ ^[a-z0-9][a-z0-9_-]{2,50}$ ]] || { echo 'Use a new simple lowercase Compose project name (3-51 characters).' >&2; exit 1; }
[[ ! -e "$destination" && ! -L "$destination" ]] || { echo 'Restore directory must not exist; nothing was overwritten.' >&2; exit 1; }
[[ "$destination" != *[[:space:]\$\#]* ]] || { echo 'Restore path cannot contain whitespace, dollar signs, or #.' >&2; exit 1; }
[[ -d "$(dirname -- "$destination")" ]] || { echo 'Create the restore parent directory first.' >&2; exit 1; }
command -v docker >/dev/null || { echo 'Docker Compose is required.' >&2; exit 1; }
existing=$(docker ps --all --quiet --filter "label=com.docker.compose.project=$project")
[[ -z "$existing" ]] || { echo 'Compose project already has containers; use a fresh project.' >&2; exit 1; }
for volume in casdoor_db caddy_data caddy_config; do
  if docker volume inspect "${project}_$volume" >/dev/null 2>&1; then
    echo "Volume ${project}_$volume already exists; use a fresh project." >&2
    exit 1
  fi
done
# Verify all archive entries before creating any destination or database.
python3 - "$archive" "$expected_lock" "$destination" "$project" <<'PY'
import hashlib, json, os, pathlib, sys, tarfile
archive, expected, destination = map(pathlib.Path, sys.argv[1:4])
project = sys.argv[4]
with tarfile.open(archive, 'r:gz') as tar:
    members = tar.getmembers()
    paths = {}
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
            raise SystemExit('Unsafe archive member; links, devices, and path traversal are refused')
        key = str(path)
        if key in paths:
            raise SystemExit('Duplicate archive path refused')
        paths[key] = member
    def content(name):
        member = paths.get(name)
        if member is None or not member.isfile():
            raise SystemExit('Backup lacks required file: ' + name)
        return tar.extractfile(member).read()
    metadata = json.loads(content('metadata.json'))
    if metadata.get('schema_version') != 1:
        raise SystemExit('Unsupported backup schema')
    lock = content('versions.lock.yaml')
    if lock != expected.read_bytes() or hashlib.sha256(lock).hexdigest() != metadata.get('versions_lock_sha256'):
        raise SystemExit('Backup does not match the explicitly supplied release lock')
    if hashlib.sha256(content('database.dump')).hexdigest() != metadata.get('database_dump_sha256'):
        raise SystemExit('Database dump checksum differs')
    for name in ('runtime/compose.env', 'deploy/compose.yaml', 'deploy/Caddyfile'):
        content(name)
    # Only ordinary files and directories remain. Use exclusive creates so an
    # unexpected existing path cannot be followed or overwritten during extraction.
    destination.mkdir(mode=0o700)
    for member in sorted(members, key=lambda item: len(pathlib.PurePosixPath(item.name).parts)):
        target = destination / pathlib.PurePosixPath(member.name)
        if member.isdir():
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open('xb') as output:
                import shutil
                shutil.copyfileobj(tar.extractfile(member), output)
            target.chmod(member.mode & 0o700)
    env = destination / 'runtime/compose.env'
    replacements = {'RUNTIME_DIR': str(destination / 'runtime'), 'RUN_UID': str(os.getuid()), 'RUN_GID': str(os.getgid()), 'COMPOSE_PROJECT_NAME': project}
    lines = []
    seen = set()
    for line in env.read_text().splitlines():
        key, separator, value = line.partition('=')
        seen.add(key)
        lines.append(key + '=' + replacements[key] if separator and key in replacements else line)
    lines.extend(key + '=' + value for key, value in replacements.items() if key not in seen)
    env.write_text('\n'.join(lines) + '\n')
    env.chmod(0o600)
PY
runtime="$destination/runtime"
compose=(docker compose --project-name "$project" --env-file "$runtime/compose.env" --file "$destination/deploy/compose.yaml" --profile '*')
"${compose[@]}" config --quiet
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
"${compose[@]}" create proxy
"${compose[@]}" cp "$destination/caddy_data/." proxy:/data/
"${compose[@]}" cp "$destination/caddy_config/." proxy:/config/
echo "Restored into $destination with isolated Compose project $project."
echo 'Only the new database is running. Inspect source/image pins and keep DNS/public ports isolated before starting applications.'
echo 'Use this restore project name on every subsequent Compose command; do not omit --project-name.'
