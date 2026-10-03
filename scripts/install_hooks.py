"""Install the git pre-commit hook that blocks personal data from commits."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = """#!/bin/sh
# Installed by scripts/install_hooks.py: blocks private files and personal details.
root="$(git rev-parse --show-toplevel)"
for py in "$root/.venv312/Scripts/python.exe" "$root/.venv/Scripts/python.exe" \
          "$root/.venv/bin/python"; do
  if [ -x "$py" ]; then exec "$py" "$root/scripts/check_private_data.py"; fi
done
exec python "$root/scripts/check_private_data.py"
"""


def main() -> int:
    hook = ROOT / ".git" / "hooks" / "pre-commit"
    hook.write_text(HOOK, encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    print(f"Installed {hook}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
