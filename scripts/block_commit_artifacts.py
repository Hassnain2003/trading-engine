"""Pre-commit hook: block committing secrets and runtime artifacts.

Exits non-zero if any staged file matches:
  *.db, *.db-*, *.db.*, *.sqlite*, data/*.csv, .env, .env.*
"""

import sys
from pathlib import Path

BLOCKED = (
    ".env",
    ".env.",
    ".db",
    ".db-",
    ".db.",
    ".sqlite",
)


def blocked(path: str) -> bool:
    p = Path(path)
    if p.name == ".env" or p.name.startswith(".env."):
        return True
    parts = p.parts
    if len(parts) >= 2 and parts[0] == "data" and parts[1].endswith(".csv"):
        return True
    for suf in BLOCKED:
        if p.name.endswith(suf) and suf != ".env":
            return True
    return False


def main() -> int:
    # In pre-commit, filenames are passed as arguments.
    bad = [a for a in sys.argv[1:] if blocked(a)]
    if bad:
        print("Refusing to commit secrets / runtime artifacts:")
        for f in bad:
            print(f"  {f}")
        print("Add them to .gitignore and remove from the index (git rm --cached).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
