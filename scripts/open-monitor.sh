#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SERVER=${1:-114.215.182.66}
USER_NAME=${2:-ecs-user}
KEY=${3:-/Users/hsjv/Downloads/key1.pem}
PORT=${4:-22}
echo '浏览器打开 http://127.0.0.1:18787；保持此窗口运行，Ctrl+C 关闭隧道。'
exec ssh -N -i "$KEY" -p "$PORT" -o BatchMode=yes -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$ROOT/data/deploy/known_hosts" -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:18787:127.0.0.1:8787 "$USER_NAME@$SERVER"
