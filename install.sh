#!/usr/bin/env bash
set -euo pipefail

REPO="https://raw.githubusercontent.com/j2h4u/oh-my-statusline/main"
DEST="$HOME/.claude/omcc-statusline.py"

function die { echo "error: $*" >&2; exit 1; }

command -v python3 >/dev/null || die "python3 not found"
command -v curl   >/dev/null || die "curl not found"

mkdir -p "$(dirname "$DEST")"
curl -sSL "$REPO/omcc-statusline.py" -o "$DEST"
chmod +x "$DEST"
python3 "$DEST" --install

echo ""
echo "Done. Restart Claude Code to see the statusline."
echo "Run 'python3 $DEST --demo' to preview."
