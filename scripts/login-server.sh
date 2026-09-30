#!/bin/sh
# Run locally. Password is entered interactively on the server, never as an argument.
set -eu
if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then
  printf '%s\n' 'Usage: sh scripts/login-server.sh SERVER SSH_USER PRIVATE_KEY_PATH [PORT]' >&2
  exit 2
fi
server=$1
ssh_user=$2
private_key=$3
ssh_port=${4:-22}
script_dir=$(dirname "$0")
exec ssh -t -i "$private_key" -p "$ssh_port" \
  -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=$script_dir/../data/deploy/known_hosts" \
  "$ssh_user@$server" \
  'cd /opt/fishfishfish && sudo docker compose run --rm login'
