#!/usr/bin/env bash
# Idempotently provision the shared cross-repository flock on a self-hosted host.
set -euo pipefail

source_config="${1:?tmpfiles source config is required}"
destination=/etc/tmpfiles.d/otzaria-pipeline.conf
expected=$'d /run/lock/otzaria 1777 root root -\nf /run/lock/otzaria/host-heavy.lock 0666 root root -'
actual="$(awk 'NF && $1 !~ /^#/ {print}' "$source_config")"
[ "$actual" = "$expected" ] || {
  echo "::error::refusing to install an unexpected host-lock tmpfiles policy"
  exit 2
}

if ! sudo test -f "$destination" || ! sudo cmp -s "$source_config" "$destination"; then
  sudo install -o root -g root -m 0644 "$source_config" "$destination"
fi
sudo systemd-tmpfiles --create "$destination"

lock=/run/lock/otzaria/host-heavy.lock
test -d /run/lock/otzaria
test -f "$lock" && test ! -L "$lock" && test -w "$lock"
[ "$(stat -c '%a:%U:%G' /run/lock/otzaria)" = '1777:root:root' ]
[ "$(stat -c '%a:%U:%G' "$lock")" = '666:root:root' ]
echo "durable host lease provisioned: $lock"
