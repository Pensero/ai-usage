#!/bin/sh
# Install git-ai-usage: curl -fsSL https://raw.githubusercontent.com/Pensero/ai-usage/main/install.sh | sh
# Run it inside a repository to also install the hooks there and backfill your past commits.
set -eu

BIN_DIR="${GIT_AI_USAGE_BIN_DIR:-$HOME/.local/bin}"
SOURCE="${GIT_AI_USAGE_SOURCE:-https://raw.githubusercontent.com/Pensero/ai-usage/main/git-ai-usage}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "git-ai-usage needs python3 (3.9 or later)." >&2
  exit 1
fi
if ! command -v git >/dev/null 2>&1; then
  echo "git-ai-usage needs git." >&2
  exit 1
fi

mkdir -p "$BIN_DIR"
curl -fsSL "$SOURCE" -o "$BIN_DIR/git-ai-usage.tmp"
chmod +x "$BIN_DIR/git-ai-usage.tmp"
mv "$BIN_DIR/git-ai-usage.tmp" "$BIN_DIR/git-ai-usage"
echo "Installed $BIN_DIR/git-ai-usage"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Add $BIN_DIR to your PATH to use 'git ai-usage'." ;;
esac

if git rev-parse --git-dir >/dev/null 2>&1; then
  "$BIN_DIR/git-ai-usage" setup
else
  echo "Run 'git ai-usage setup' in each repository where you want the notes."
fi
