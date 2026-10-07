"""Where the tailored resume's content and look come from.

Two private files drive the resume builder:

- the master CV (``MASTER_CV_PATH``, default ``profile/master_cv.pdf``): every job, project,
  and activity the candidate has done, the pool each tailored resume picks from;
- the format example (``RESUME_FORMAT_PATH``, default the ``RESUME_PATH`` resume): a finished
  one-page resume whose look is copied (font, text size, and section headings in order), but
  whose content is not preferred over the master CV's.

Both stay in git-ignored paths; only their layout and text are read, on this computer.
"""

import logging
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from agent.settings import PROJECT_ROOT

LOGGER = logging.getLogger(__name__)
DEFAULT_MASTER_CV = PROJECT_ROOT / "profile" / "master_cv.pdf"
MIN_BODY_SIZE, MAX_BODY_SIZE = 10.0, 12.0

# Section roles, checked in this order: "Leadership Experience" is activities, not experience.
EDUCATION, PROJECTS, ACTIVITIES, SKILLS, EXPERIENCE = (
    "education",
    "projects",
    "activities",
    "skills",
    "experience",
)
_ROLES = (
    (EDUCATION, re.compile(r"\beducation\b", re.IGNORECASE)),
    (PROJECTS, re.compile(r"\bprojects?\b", re.IGNORECASE)),
    (ACTIVITIES, re.compile(r"\b(?:leadership|activities|involvement|extracurriculars?)\b", re.I)),
    (SKILLS, re.compile(r"\bskills\b", re.IGNORECASE)),
    (EXPERIENCE, re.compile(r"\b(?:experience|employment|work history)\b", re.IGNORECASE)),
)
# PDF font families Word has under another name; anything unknown falls back to the default.
_FONTS = {
    "times": "Times New Roman",
    "timesnewroman": "Times New Roman",
    "helvetica": "Arial",
    "arial": "Arial",
    "calibri": "Calibri",
    "cambria": "Cambria",
    "garamond": "Garamond",
    "ebgaramond": "Garamond",
    "georgia": "Georgia",
    "verdana": "Verdana",
    "bookantiqua": "Book Antiqua",
    "palatino": "Palatino Linotype",
    "palatinolinotype": "Palatino Linotype",
    "aptos": "Aptos",
}


@dataclass(frozen=True)
class ResumeFormat:
    """The look of the resume: font, body text size, and section headings in order."""

    font: str = "Times New Roman"
    body_size: float = 11.0
    name_size: float = 15.0
    sections: tuple[tuple[str, str], ...] = (
        (EDUCATION, "Education"),
        (EXPERIENCE, "Experience"),
        (PROJECTS, "Technical Projects"),
        (SKILLS, "Skills"),
    )
    uppercase: bool = True  # headings are shown in capitals
    source: str = ""  # the file the format was read from, empty for the default

    def heading(self, role: str) -> str | None:
        """The heading this format uses for a role, or None when it has no such section."""
        return next((text for found, text in self.sections if found == role), None)

    def heading_for_kind(self, kind: str) -> str:
        """Where an entry of this kind goes: projects and activities fall back to experience."""
        role = {"project": PROJECTS, "activity": ACTIVITIES, "leadership": ACTIVITIES}.get(
            kind, EXPERIENCE
        )
        return (
            self.heading(role)
            or self.heading(EXPERIENCE)
            or next(text for found, text in self.sections if found not in (EDUCATION, SKILLS))
        )

    @property
    def entry_headings(self) -> list[str]:
        return [text for role, text in self.sections if role not in (EDUCATION, SKILLS)]


DEFAULT_FORMAT = ResumeFormat()


@dataclass(frozen=True)
class ResumeSources:
    """The master CV's text and the format to copy, as the resume builder uses them."""

    master_cv_text: str = ""
    layout: ResumeFormat = field(default_factory=ResumeFormat)
    notes: tuple[str, ...] = ()


def read_format(path: Path) -> ResumeFormat:
    """Read the font, body size, name size, and section headings from a PDF resume."""
    from pypdf import PdfReader

    pieces: list[tuple[str, str, float, float]] = []  # text, font, size, baseline

    def visit(text: str, cm: list[float], tm: list[float], font: dict | None, size: float):
        if not text.strip() or not font:
            return
        scale = math.hypot(tm[1], tm[3]) * math.hypot(cm[1], cm[3])
        baseline = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        pieces.append((text, str(font.get("/BaseFont", "")), size * scale, baseline))

    reader = PdfReader(str(path))
    reader.pages[0].extract_text(visitor_text=visit)
    if not pieces:
        raise ValueError(f"No readable text in the format example {path.name}.")

    weights: Counter[float] = Counter()
    fonts: Counter[str] = Counter()
    for text, font, size, _y in pieces:
        weights[round(size * 2) / 2] += len(text.strip())
        fonts[_family(font)] += len(text.strip())
    body = max(weights, key=lambda size: (weights[size], -size))
    family = next((_FONTS[name] for name, _count in fonts.most_common() if name in _FONTS), None)

    lines: dict[int, list[tuple[str, float]]] = {}
    for text, _font, size, y in pieces:
        lines.setdefault(round(-y), []).append((text, size))
    ordered = [lines[key] for key in sorted(lines)]
    name_size = max(size for _text, size in ordered[0])

    sections: list[tuple[str, str]] = []
    for parts in ordered[1:]:
        text = " ".join("".join(part for part, _size in parts).split()).rstrip(":")
        role = _role(text)
        if role and role not in {found for found, _ in sections}:
            sections.append((role, text))
    if not any(role not in (EDUCATION, SKILLS) for role, _ in sections):
        raise ValueError(f"No Experience or Projects heading in the format example {path.name}.")
    uppercase = all(text.isupper() for _role, text in sections)
    if not any(role == EDUCATION for role, _ in sections):
        sections.insert(0, DEFAULT_FORMAT.sections[0])
    if not any(role == SKILLS for role, _ in sections):
        sections.append(DEFAULT_FORMAT.sections[-1])

    return ResumeFormat(
        font=family or DEFAULT_FORMAT.font,
        body_size=min(MAX_BODY_SIZE, max(MIN_BODY_SIZE, body)),
        name_size=max(name_size, body + 2),
        sections=tuple(sections),
        uppercase=uppercase,
        source=path.name,
    )


def load_resume_sources() -> ResumeSources:
    """The master CV and the format example named in .env, or the defaults, with notes."""
    from agent.applier.cli import default_resume_path
    from agent.applier.greenhouse import extract_resume_text

    load_dotenv(PROJECT_ROOT / ".env")
    notes: list[str] = []
    master = _env_path("MASTER_CV_PATH") or DEFAULT_MASTER_CV
    master_text = ""
    if master.is_file():
        try:
            master_text = extract_resume_text(master)
        except ValueError as error:
            notes.append(f"Could not read the master CV: {error}")
    else:
        notes.append(
            f"No master CV at {_shown(master)}: the resume and library are the only sources."
        )
    example = _env_path("RESUME_FORMAT_PATH") or default_resume_path()
    layout = DEFAULT_FORMAT
    if example.is_file():
        try:
            layout = read_format(example)
        except Exception as error:  # a format we cannot read must not stop the resume
            notes.append(f"Used the default format: {error}")
    else:
        notes.append(f"No format example at {_shown(example)}: used the default format.")
    for note in notes:
        LOGGER.info(note)
    return ResumeSources(master_text, layout, tuple(notes))


def _role(line: str) -> str | None:
    """The section a heading line names, or None for any other line."""
    words = line.split()
    if not 1 <= len(words) <= 4 or any(char.isdigit() or char in ":|,•" for char in line):
        return None
    if not (line.isupper() or all(word[:1].isupper() or word in {"&", "and"} for word in words)):
        return None
    return next((role for role, pattern in _ROLES if pattern.search(line)), None)


def _family(base_font: str) -> str:
    """'ABCDEF+TimesNewRomanPS-BoldMT' -> 'timesnewroman'; 'Calibri-Bold' -> 'calibri'."""
    name = base_font.lstrip("/").split("+")[-1]
    name = re.split(r"[-,]", name)[0]
    name = re.sub(r"(?:PSMT|PS|MT)$", "", name).casefold()
    if name in _FONTS:
        return name
    return re.sub(r"(?:bold|italic|oblique|regular|roman)+$", "", name) or name


def _env_path(name: str) -> Path | None:
    configured = os.getenv(name, "").strip()
    if not configured:
        return None
    path = Path(configured).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _shown(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.name
