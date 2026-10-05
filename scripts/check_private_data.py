"""Pre-commit guard: block private files and personal details from being committed.

Personal values are read from the local, git-ignored profile at commit time; they are never
written anywhere, and findings are reported only by category.
"""

import fnmatch
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRIVATE_PATTERNS = (
    "profile/profile.yaml",
    "profile/*.pdf",
    "profile/resume*",
    "profile/library/*",
    ".env",
    ".env.*",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "data/*",
    "screenshots/*",
    "reviews/*",
    "dashboard.html",
    ".playwright/*",
    "playwright/.auth/*",
    "auth/*",
)
ALLOWED_PATHS = frozenset({".env.example", "profile/profile.example.yaml"})
MIN_VALUE_LENGTH = 4


def private_paths(paths: Iterable[str]) -> list[str]:
    """Return staged paths that must never be committed."""
    blocked = []
    for path in paths:
        normalized = path.replace("\\", "/")
        if normalized in ALLOWED_PATHS:
            continue
        if any(fnmatch.fnmatch(normalized, pattern) for pattern in PRIVATE_PATTERNS):
            blocked.append(normalized)
    return blocked


def personal_values(profile: Mapping[str, Any], resume_path: str | None = None) -> dict[str, str]:
    """Collect identifying values from the profile, keyed by a printable category."""
    values: dict[str, str] = {}
    personal = profile.get("personal") or {}
    answers = profile.get("application_answers") or {}
    if isinstance(personal, Mapping):
        for key in ("name", "email", "phone", "linkedin", "github", "website", "location"):
            if personal.get(key):
                values[f"personal.{key}"] = str(personal[key])
        for index, part in enumerate(str(personal.get("name") or "").split()):
            values[f"personal.name part {index + 1}"] = part
        for key in ("linkedin", "github", "website"):
            handle = str(personal.get(key) or "").rstrip("/").rsplit("/", 1)[-1]
            if handle and "." not in handle:
                values[f"personal.{key} handle"] = handle
        email = str(personal.get("email") or "")
        if "@" in email:
            values["personal.email user"] = email.split("@", 1)[0]
    if isinstance(answers, Mapping):
        for key in ("current_company", "current_location", "earliest_start_date"):
            if answers.get(key):
                values[f"application_answers.{key}"] = str(answers[key])
    for section, field in (("experience", "company"), ("education", "institution")):
        for index, item in enumerate(profile.get(section) or []):
            if isinstance(item, Mapping) and item.get(field):
                values[f"{section}[{index}].{field}"] = str(item[field])
    if resume_path:
        values["resume file name"] = Path(resume_path).name
    return {
        category: value.strip()
        for category, value in values.items()
        if len(value.strip()) >= MIN_VALUE_LENGTH
    }


def find_personal_details(added_lines: Iterable[str], values: Mapping[str, str]) -> list[str]:
    """Return the categories of personal values that appear in added lines."""
    found: list[str] = []
    for line in added_lines:
        lowered = line.casefold()
        digits = re.sub(r"\D", "", line)
        for category, value in values.items():
            if category in found:
                continue
            if category == "personal.phone":
                phone = re.sub(r"\D", "", value)[-10:]
                if len(phone) >= 7 and phone in digits:
                    found.append(category)
            elif re.search(rf"(?<!\w){re.escape(value.casefold())}(?!\w)", lowered):
                found.append(category)
    return found


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
        encoding="utf-8", errors="replace",
    ).stdout


def _resume_path() -> str | None:
    configured = os.environ.get("RESUME_PATH")
    env_file = PROJECT_ROOT / ".env"
    if not configured and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("RESUME_PATH="):
                configured = line.split("=", 1)[1].strip().strip("'\"")
    return configured or None


def main() -> int:
    staged = [path for path in _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
              .splitlines() if path]
    problems: list[str] = []
    for path in private_paths(staged):
        problems.append(f"private file staged: {path}")

    profile_file = PROJECT_ROOT / "profile" / "profile.yaml"
    profile = {}
    if profile_file.is_file():
        profile = yaml.safe_load(profile_file.read_text(encoding="utf-8")) or {}
    values = personal_values(profile, _resume_path())
    if values:
        diff = _git("diff", "--cached", "--unified=0", "--no-color")
        added = [
            line[1:] for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        for category in find_personal_details(added, values):
            problems.append(f"personal detail in staged changes: {category}")

    if problems:
        print("Commit blocked to protect personal data:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("Unstage or remove these before committing.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
