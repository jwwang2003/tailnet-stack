#!/usr/bin/env bash
# Run on the laptop: remote loopback -> SSH tunnel -> local HTTP/mixed proxy.
set -euo pipefail
usage() {
  cat <<'EOF'
Usage: remote-proxy-shell.sh [--local-port PORT] [--remote-port PORT] [--ssh-port PORT] USER@HOST

Defaults: local HTTP/mixed proxy 127.0.0.1:7890, remote loopback port 17890.
SSH port comes from your SSH configuration unless --ssh-port is supplied.
Opens an interactive remote Bash session with upper/lowercase proxy variables.
Exiting the session closes the tunnel. No persistent shell/system configuration changes.
EOF
}
fail() { echo "Error: $*" >&2; exit 2; }
port() {
  [[ "$1" =~ ^[0-9]{1,5}$ ]] || fail 'Port must be an integer from 1 to 65535.'
  local number=$((10#$1))
  ((number >= 1 && number <= 65535)) || fail 'Port must be an integer from 1 to 65535.'
  printf '%d' "$number"
}
local_port=7890
remote_port=17890
ssh_port=
destination=
while (($#)); do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --local-port|--remote-port|--ssh-port)
      (($# >= 2)) || fail "Missing value for $1"
      value=$(port "$2")
      case "$1" in
        --local-port) local_port=$value ;;
        --remote-port) remote_port=$value ;;
        --ssh-port) ssh_port=$value ;;
      esac
      shift 2 ;;
    -*) fail "Unknown option: $1" ;;
    *)
      [[ -z "$destination" ]] || fail 'Supply exactly one SSH destination.'
      destination=$1
      shift ;;
  esac
done
[[ -n "$destination" ]] || { usage >&2; exit 2; }
# Accept ordinary hosts, SSH aliases and user@[IPv6]; reject command metacharacters.
host_pattern='^[a-zA-Z0-9_][][a-zA-Z0-9_.@:%-]*$'
[[ "$destination" =~ $host_pattern ]] || fail 'Use an SSH alias or user@host (configure jump hosts/keys in ~/.ssh/config).'
command -v ssh >/dev/null || fail 'OpenSSH client is required.'
proxy="http://127.0.0.1:$remote_port"
bypass='localhost,127.0.0.1,::1,db,casdoor,headscale,headplane,worker'
options=(-tt -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3
  -o ControlMaster=no -o ControlPath=none
  -R "127.0.0.1:$remote_port:127.0.0.1:$local_port")
if [[ -n "$ssh_port" ]]; then options+=(-p "$ssh_port"); fi
printf 'Forwarding remote 127.0.0.1:%s to local 127.0.0.1:%s. Exit the shell to disconnect.\n' "$remote_port" "$local_port"
# Only fixed strings and validated integer ports enter the remote shell command.
exec ssh "${options[@]}" -- "$destination" \
  "exec env http_proxy=$proxy https_proxy=$proxy all_proxy=$proxy HTTP_PROXY=$proxy HTTPS_PROXY=$proxy ALL_PROXY=$proxy no_proxy=$bypass NO_PROXY=$bypass bash -l"
