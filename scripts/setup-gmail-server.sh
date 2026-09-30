#!/bin/sh
set -eu
if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then
  printf '%s\n' 'Usage: sh scripts/setup-gmail-server.sh SERVER SSH_USER PRIVATE_KEY_PATH [PORT]' >&2
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
  'cd /opt/fishfishfish && sudo docker compose run --rm -v "$PWD/scripts/setup-gmail.py:/app/setup-gmail.py:ro" --entrypoint python login /app/setup-gmail.py && sudo docker compose run --rm trader email-test'
