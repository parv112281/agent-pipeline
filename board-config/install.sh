#!/usr/bin/env bash
# Install the board's routes and the build-sandbox settings (spec section 20.1), and mise.
# Run from anywhere: board-config/install.sh. Then restart: systemctl --user restart agent-dispatcher
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
board="$HOME/agent-pipeline/board/agent"
stamp="$(date +%Y%m%d-%H%M%S)"

# mise provides each project's toolchain (mise.toml); the dispatcher's PATH includes ~/bin.
if ! command -v "$HOME/bin/mise" >/dev/null 2>&1; then
  tmp="$(mktemp -d)"
  "$HOME/bin/gh" release download -R jdx/mise --pattern 'mise-v*-linux-x64' --pattern 'SHASUMS256.txt' -D "$tmp"
  bin="$(ls "$tmp"/mise-v*-linux-x64)"
  line="$(grep -E " (\./)?$(basename "$bin")\$" "$tmp/SHASUMS256.txt" || true)"
  if [ -z "$line" ] || ! (cd "$tmp" && echo "$line" | sha256sum -c --status -); then
    echo "mise checksum did not verify; not installing" >&2; rm -rf "$tmp"; exit 1
  fi
  install -m 0755 "$bin" "$HOME/bin/mise"
  rm -rf "$tmp"
fi
"$HOME/bin/mise" --version

# Caches the sandbox may write to must exist, or the sandbox cannot bind them.
mkdir -p "$HOME/.m2" "$HOME/.gradle" "$HOME/.npm" "$HOME/.cache" "$HOME/.local/share/mise" "$HOME/.local/state/mise"

for name in routes.json sandbox-settings.json; do
  if [ -f "$board/$name" ]; then cp "$board/$name" "$board/$name.bak-$stamp"; fi
  sed "s|__HOME__|$HOME|g" "$here/$name" > "$board/$name.new"
  python3 -m json.tool "$board/$name.new" >/dev/null
  mv "$board/$name.new" "$board/$name"
done

# The board's own safety check on the routes file (no --dangerously-skip-permissions, no default route).
(cd "$HOME/agent-pipeline/board" && "$HOME/.nvm/versions/node/v20.18.0/bin/node" --env-file=.env agent/prestart.mjs --check-routes >/dev/null) \
  || { echo "routes check failed; restoring the backup" >&2
       for name in routes.json sandbox-settings.json; do
         [ -f "$board/$name.bak-$stamp" ] && cp "$board/$name.bak-$stamp" "$board/$name"
       done
       exit 1; }

echo "Installed. Backups: $board/*.bak-$stamp"
echo "Restart the dispatcher to use them: systemctl --user restart agent-dispatcher"
