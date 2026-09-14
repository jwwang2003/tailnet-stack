#!/usr/bin/env python3
"""Merge configured Docker mirror hosts into daemon NO_PROXY; preview unless --apply."""
import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit


def merge(config, info, environment):
    if not isinstance(config, dict) or not isinstance(info, dict):
        raise ValueError('Docker configuration and info must be JSON objects')
    result = copy.deepcopy(config)
    proxies = result.setdefault('proxies', {})
    if not isinstance(proxies, dict):
        raise ValueError('daemon proxies must be an object')
    registry = info.get('RegistryConfig') or {}
    if not isinstance(registry, dict):
        raise ValueError('Docker RegistryConfig must be an object')
    mirrors = []
    for values in (config.get('registry-mirrors', []), registry.get('Mirrors') or []):
        if not isinstance(values, list):
            raise ValueError('Registry mirrors must be an array')
        for value in values:
            if not isinstance(value, str):
                raise ValueError('Invalid registry mirror URL')
            url = urlsplit(value)
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError('Mirrors must be HTTP(S) URLs without credentials/query/fragment')
            # Host-wide exclusion works across ports; use literal hosts, not *.aliyuncs.com.
            host = url.hostname.lower()
            if any(c.isspace() for c in host) or any(c in host for c in ',*'):
                raise ValueError('Invalid registry mirror hostname')
            mirrors.append(host)
    entries, seen = [], set()
    for value in ('localhost,127.0.0.1,::1', proxies.get('no-proxy', ''), info.get('NoProxy', ''),
                  environment.get('NO_PROXY', ''), environment.get('no_proxy', ''), ','.join(mirrors)):
        if not isinstance(value, str):
            raise ValueError('NO_PROXY values must be strings')
        for entry in value.split(','):
            entry = entry.strip()
            if any(c in entry for c in '\r\n\0'):
                raise ValueError('Invalid control character in NO_PROXY')
            if entry and entry.lower() not in seen:
                entries.append(entry)
                seen.add(entry.lower())
    proxies['no-proxy'] = ','.join(entries)
    return result, sorted(set(mirrors))


def apply(path, original, updated):
    """Validate before publishing; preserve a private byte-for-byte backup."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, candidate = tempfile.mkstemp(prefix='.docker-mirror-proxy-', suffix='.json', dir=path.parent)
    candidate = Path(candidate)
    backup = None
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(updated, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        subprocess.run(['dockerd', '--validate', '--config-file', str(candidate)],
                       check=True, capture_output=True, text=True)
        current = path.read_bytes() if path.exists() else None
        if current != original:
            raise ValueError('daemon.json changed during preparation; rerun instead of overwriting')
        if original is not None:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = path.with_name(path.name + '.before-mirror-no-proxy.' + stamp)
            with backup.open('xb') as stream:
                os.chmod(backup, 0o600)
                stream.write(original)
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--daemon-config', type=Path, default=Path('/etc/docker/daemon.json'))
    parser.add_argument('--configured-only', action='store_true', help='Skip running Docker info; use only this file and shell exclusions')
    parser.add_argument('--apply', action='store_true', help='Validate and write daemon.json; does not restart Docker')
    args = parser.parse_args()
    try:
        if args.daemon_config.is_symlink():
            raise ValueError('daemon.json is a symlink; use its real target path explicitly')
        original = args.daemon_config.read_bytes() if args.daemon_config.exists() else None
        config = json.loads(original) if original is not None else {}
        info = {}
        if not args.configured_only:
            # This recipe must run against the host daemon, not a selected remote context.
            raw = subprocess.check_output(['docker', '--host', 'unix:///var/run/docker.sock', 'info', '--format', '{{json .}}'], text=True, stderr=subprocess.PIPE)
            info = json.loads(raw)
        updated, mirrors = merge(config, info, os.environ)
        print('Mirror hosts:', ', '.join(mirrors) or '(none found)')
        print('NO_PROXY:', updated['proxies']['no-proxy'])
        if not args.apply:
            print('Preview only. Rerun with sudo and --apply to save; restart Docker separately.')
            return 0
        if updated == config:
            print('Already synchronized; no file changes.')
            return 0
        backup = apply(args.daemon_config, original, updated)
        if backup:
            print('Backup:', backup)
        print('Saved daemon proxies.no-proxy. HTTP/HTTPS proxy URLs and other settings preserved.')
        print('Restart Docker when appropriate, then verify: docker info --format "{{.NoProxy}}"')
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            print('Failed to read Docker info or validate candidate configuration; no configuration was published.')
            print('Check the local daemon/configuration; use --configured-only if Docker is stopped.')
        else:
            print('Error:', error)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
