#!/usr/bin/env bash
set -euo pipefail

REPO="https://raw.githubusercontent.com/j2h4u/oh-my-statusline/main"
DEST="$HOME/.claude/omcc-statusline.py"

function die { echo "error: $*" >&2; exit 1; }

command -v curl >/dev/null || die "curl not found"

if command -v uv >/dev/null; then
    PY="uv run"
elif command -v python3 >/dev/null; then
    PY="python3"
else
    die "neither uv nor python3 found"
fi

mkdir -p "$(dirname "$DEST")"
curl -sSL "$REPO/omcc-statusline.py" -o "$DEST"
chmod +x "$DEST"
$PY "$DEST" --install

echo ""
echo "Done. Restart Claude Code to see the statusline."
echo "Run '$PY $DEST --demo' to preview."
