#!/usr/bin/env bash
# StackWeft installer: create the home dir + secrets template, and put `sw` on PATH.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # repo root (contains sw + the package)
HOME_DIR="${STACKWEFT_HOME:-$HOME/.stackweft}"

mkdir -p "$HOME_DIR/data" "$HOME_DIR/logs"
chmod 700 "$HOME_DIR" 2>/dev/null || true

SECRETS="$HOME_DIR/secrets.env"
if [ ! -f "$SECRETS" ]; then
  cat > "$SECRETS" <<'EOF'
# StackWeft credentials — fill in and keep private.
STACKWEFT_BASE=https://your-gateway/
STACKWEFT_TASK_KEY=

# Optional top tier (OpenAI Responses wire API):
# CODEX_URL=
# CODEX_KEY=
# CODEX_MODEL=gpt-5.5

# Language for AI <-> user communication (default 中文):
# STACKWEFT_LANG=中文
EOF
  chmod 600 "$SECRETS"
  echo "created $SECRETS  — fill in STACKWEFT_BASE + STACKWEFT_TASK_KEY"
else
  echo "kept existing $SECRETS"
fi

RC="${HOME}/.bashrc"
MARK="# >>> stackweft >>>"
if ! grep -qF "$MARK" "$RC" 2>/dev/null; then
  cat >> "$RC" <<EOF

$MARK
export STACKWEFT_HOME="$HOME_DIR"
export PATH="$HERE:\$PATH"
# <<< stackweft <<<
EOF
  echo "added STACKWEFT_HOME + sw to PATH in $RC"
else
  echo "$RC already configured"
fi

echo
echo "done. open a new shell (or: source $RC), then:"
echo "    sw run \"<your requirement>\""
