#!/bin/sh
set -eu
config=/root/jlu-milvus/caddy/Caddyfile
backup=/opt/knowledgeos/Caddyfile.before-knowledgeos
if ! grep -q '^knowledge.jlubot.asia {' "$config"; then
  test ! -e "$backup" || { echo 'Backup already exists; inspect before modifying Caddy.'; exit 1; }
  cp -p "$config" "$backup"
  cat >> "$config" <<'CADDY'

knowledge.jlubot.asia {
    encode zstd gzip
    reverse_proxy 127.0.0.1:18081
}
CADDY
fi
if docker exec milvus-caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; then
  docker exec milvus-caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
else
  cp -p "$backup" "$config"
  exit 1
fi
