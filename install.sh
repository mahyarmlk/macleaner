#!/bin/sh
# Puts `macleaner` on your PATH by linking it into ~/.local/bin.
set -e
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.local/bin"
chmod +x "$here/macleaner.py"
ln -sf "$here/macleaner.py" "$HOME/.local/bin/macleaner"
echo "Installed: $HOME/.local/bin/macleaner -> $here/macleaner.py"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "Add this to your ~/.zshrc:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
