#!/bin/sh
# Install git ai-usage for all the users of a Mac. Run it as root from the MDM (Jamf, Kandji,
# Fleet, Intune for macOS). Run it again at each check-in: it is safe to run many times, and
# it also sets up the users that were added after the last run.
#
# REMOTE: the hooks run only in repositories whose push URL starts with this host and path. Put the
# organization of your git provider here, for example "github.com/acme/". Separate two or
# more values with a space. Set REMOTE="" to run in all repositories.
REMOTE="${GIT_AI_USAGE_REMOTE-github.com/REPLACE_ORG/}"
SOURCE="${GIT_AI_USAGE_SOURCE:-https://raw.githubusercontent.com/Pensero/ai-usage/main/git-ai-usage}"
BIN=/usr/local/bin/git-ai-usage
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this script as root." >&2
  exit 1
fi
case "$REMOTE" in
  *REPLACE_ORG*) echo "Set REMOTE to the organization of your git provider, for example github.com/acme/." >&2; exit 1 ;;
esac
if ! xcode-select -p >/dev/null 2>&1; then
  echo "git ai-usage needs git and python3: install the Command Line Tools (xcode-select --install)." >&2
  exit 1
fi

mkdir -p "$(dirname "$BIN")"
if [ -f "$SOURCE" ]; then
  cp "$SOURCE" "$BIN.tmp"
else
  curl -fsSL "$SOURCE" -o "$BIN.tmp"
fi
chmod 755 "$BIN.tmp"
mv "$BIN.tmp" "$BIN"

flags=""
for value in $REMOTE; do
  flags="$flags --remote $value"
done
for user in $(dscl . list /Users UniqueID | awk '$2 >= 501 { print $1 }'); do
  home="$(dscl . read "/Users/$user" NFSHomeDirectory | awk '{ print $2 }')"
  [ -d "$home" ] || continue
  # shellcheck disable=SC2086
  sudo -u "$user" -H "$BIN" install --global $flags
done
