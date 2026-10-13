"""The candidate's public GitHub repositories, as evidence for the resume sub-agent.

When a job asks for a skill the resume, master CV, and experience bank never mention, the
candidate may still have used it in a project on GitHub. This module reads the candidate's
public repositories (name, description, topics, main language, the libraries named in
requirements.txt, pyproject.toml, or package.json, and the start of the README) and turns
each into a source document. The resume sub-agent searches them like any other document, and
every bullet written from one still has to quote it word for word.

The GitHub user comes from GITHUB_USERNAME, or else from personal.github in the profile.
Only public repositories are read, through GitHub's API and raw file host; GITHUB_TOKEN, when
set, only raises the rate limit. The result is cached for a day in data/github/ (git-ignored),
so a run of several kits asks GitHub once.
"""

import json
import logging
import os
import re
import tomllib
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml

from agent.library import LibraryDocument
from agent.settings import PROJECT_ROOT

LOGGER = logging.getLogger(__name__)
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
CACHE_DIR = PROJECT_ROOT / "data" / "github"
CACHE_HOURS = 24
MAX_REPOS = 30
README_CHARS = 4000
DEPENDENCY_FILES = ("requirements.txt", "pyproject.toml", "package.json")
DOC_PREFIX = "github:"
_USER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def github_user(profile_path: Path | None = None) -> str | None:
    """The candidate's GitHub login: GITHUB_USERNAME, or the profile's personal.github URL."""
    name = os.environ.get("GITHUB_USERNAME", "").strip()
    if not name:
        path = profile_path or PROJECT_ROOT / "profile" / "profile.yaml"
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return None
        personal = data.get("personal") if isinstance(data, dict) else None
        url = str((personal or {}).get("github") or "") if isinstance(personal, dict) else ""
        name = url.strip().rstrip("/").rsplit("/", 1)[-1]
    return name if _USER.match(name) else None


def load_github_documents(
    user: str,
    *,
    client: httpx.Client | None = None,
    cache_dir: Path | None = None,
    now: datetime | None = None,
) -> list[LibraryDocument]:
    """One document per public, non-fork repository, from the cache when it is a day old or less.

    Any failure is logged and returns what is cached (even when stale) or nothing, so the
    resume is still made from the other documents.
    """
    now = now or datetime.now(UTC)
    path = (cache_dir or CACHE_DIR) / f"{user.casefold()}.json"
    cached = _read_cache(path)
    if cached is not None:
        fetched = datetime.fromisoformat(cached["fetched_at"])
        if now - fetched <= timedelta(hours=CACHE_HOURS):
            return _documents(cached["repos"])
    http = client or httpx.Client(timeout=20.0, follow_redirects=True)
    try:
        repos = fetch_repositories(user, http)
    except httpx.HTTPError as error:
        LOGGER.warning("Could not read GitHub repositories: %s", error)
        return _documents(cached["repos"]) if cached is not None else []
    finally:
        if client is None:
            http.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"fetched_at": now.isoformat(), "repos": repos}), encoding="utf-8"
    )
    return _documents(repos)


def fetch_repositories(user: str, http: httpx.Client) -> list[dict[str, Any]]:
    """The user's public repositories with their README and the libraries they depend on."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = http.get(
        f"{API}/users/{user}/repos",
        params={"per_page": 100, "sort": "pushed", "type": "owner"},
        headers=headers,
    )
    response.raise_for_status()
    repos = []
    for raw in response.json():
        if not isinstance(raw, dict) or raw.get("fork") or not raw.get("name"):
            continue
        name = str(raw["name"])
        repos.append(
            {
                "name": name,
                "url": str(raw.get("html_url") or f"https://github.com/{user}/{name}"),
                "description": str(raw.get("description") or ""),
                "language": str(raw.get("language") or ""),
                "topics": [str(topic) for topic in raw.get("topics") or []],
                "readme": _readme(http, user, name, headers),
                "libraries": {
                    file: libraries
                    for file in DEPENDENCY_FILES
                    if (libraries := _libraries(http, user, name, file))
                },
            }
        )
        if len(repos) >= MAX_REPOS:
            break
    return repos


def _readme(http: httpx.Client, user: str, repo: str, headers: dict[str, str]) -> str:
    response = http.get(
        f"{API}/repos/{user}/{repo}/readme",
        headers={**headers, "Accept": "application/vnd.github.raw"},
    )
    return response.text[:README_CHARS] if response.status_code == 200 else ""


def _libraries(http: httpx.Client, user: str, repo: str, file: str) -> list[str]:
    response = http.get(f"{RAW}/{user}/{repo}/HEAD/{file}")
    if response.status_code != 200:
        return []
    try:
        return dependency_names(file, response.text)
    except (ValueError, tomllib.TOMLDecodeError):
        return []


def dependency_names(file: str, text: str) -> list[str]:
    """The library names a dependency file lists, lowercased, with "_" written as "-".

    "-" keeps a package such as langchain_openai findable as "langchain".
    """
    names: list[str] = []
    if file == "package.json":
        data = json.loads(text)
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            names += list((data.get(key) or {}) if isinstance(data, dict) else {})
    elif file == "pyproject.toml":
        data = tomllib.loads(text)
        project = data.get("project") or {}
        specs = list(project.get("dependencies") or [])
        for group in (project.get("optional-dependencies") or {}).values():
            specs += list(group)
        poetry = (data.get("tool") or {}).get("poetry") or {}
        specs += [name for name in poetry.get("dependencies") or {} if name != "python"]
        names += [_requirement(str(spec)) for spec in specs]
    else:
        for line in text.splitlines():
            if line.strip().startswith(("#", "-")):
                continue
            names.append(_requirement(line))
    kept: list[str] = []
    for name in names:
        name = name.strip().casefold().replace("_", "-")
        if name and name not in kept:
            kept.append(name)
    return kept


def _requirement(spec: str) -> str:
    match = _REQUIREMENT.match(spec)
    return match.group(1) if match else ""


def _documents(repos: Iterable[dict[str, Any]]) -> list[LibraryDocument]:
    documents = []
    for repo in repos:
        lines = [f"{repo['name']} (GitHub repository: {repo['url']})"]
        if repo.get("description"):
            lines.append(f"Description: {repo['description']}")
        if repo.get("language"):
            lines.append(f"Main language: {repo['language']}")
        if repo.get("topics"):
            lines.append(f"Topics: {', '.join(repo['topics'])}")
        for file, libraries in (repo.get("libraries") or {}).items():
            lines.append(f"Libraries used ({file}): {', '.join(libraries)}")
        if repo.get("readme"):
            lines += ["README:", str(repo["readme"]).strip()]
        documents.append(
            LibraryDocument(
                f"{DOC_PREFIX}{repo['name']}", f"GitHub repository {repo['name']}", "\n".join(lines)
            )
        )
    return documents


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        datetime.fromisoformat(data["fetched_at"])
        return data if isinstance(data.get("repos"), list) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
