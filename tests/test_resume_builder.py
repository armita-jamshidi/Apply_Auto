"""The resume builder: content from the master CV, look copied from the format example."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from docx import Document

from agent import resume_format, resume_tailor
from agent.resume_format import (
    ACTIVITIES,
    EDUCATION,
    EXPERIENCE,
    PROJECTS,
    SKILLS,
    ResumeFormat,
    load_resume_sources,
    read_format,
)
from agent.resume_tailor import (
    KEYWORD_PROMPT,
    POOL_PROMPT,
    Entry,
    Keywords,
    TailoredResume,
    tailor_resume,
    write_docx,
)

# A fictional one-page resume: name, contact, then headings and body lines.
FORMAT_LINES = [
    ("Helvetica-Bold", 16, "Jordan Example"),
    ("Helvetica", 10.5, "jordan@example.com | Raleigh, NC"),
    ("Helvetica-Bold", 11, "Education"),
    ("Helvetica", 10.5, "Example State University, B.S. Computer Science, Dec 2025"),
    ("Helvetica-Bold", 11, "Work Experience"),
    ("Helvetica-Bold", 10.5, "Software Intern | Example Co"),
    ("Helvetica", 10.5, "Built a search service in Python used by the support team every day"),
    ("Helvetica-Bold", 11, "Projects"),
    ("Helvetica", 10.5, "Wrote an agent that sorts support tickets with an LLM and a rules layer"),
    ("Helvetica-Bold", 11, "Leadership & Activities"),
    ("Helvetica", 10.5, "Led a club of forty students that ships one open source tool each term"),
    ("Helvetica-Bold", 11, "Technical Skills"),
    ("Helvetica", 10.5, "Technical Skills: Python, SQL"),
]
MASTER_CV = """Jordan Example
Experience
Software Intern, Example Co
Built a search service in Python used by the support team every day.
Research Assistant, Example Lab
Evaluated LLM agents in Python on 40 tasks with an evaluation harness.
Activities
Hack Club Lead
Organized a hackathon for 200 students."""
RESUME = """Jordan Example
jordan@example.com | Raleigh, NC
Example State University, B.S. Computer Science, Dec 2025
Software Intern, Example Co
Built a search service in Python used by the support team every day."""
JOB = {
    "company": "Acme",
    "title": "AI Engineer",
    "description": "You will evaluate LLM agents in Python and organize team events.",
}


def make_pdf(path: Path, lines: list[tuple[str, float, str]], *, unit_size: bool = False) -> Path:
    """Write a one-page PDF with one line of text per entry, top to bottom.

    unit_size sets the size through the text matrix with a 1 pt font, as LaTeX and some Word
    exports do, instead of through the font size.
    """
    fonts = sorted({font for font, _size, _text in lines})
    names = {font: f"F{index}" for index, font in enumerate(fonts)}
    ops = []
    for index, (font, size, text) in enumerate(lines):
        y = 740 - 18 * index
        text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if unit_size:
            ops.append(f"BT /{names[font]} 1 Tf {size} 0 0 {size} 50 {y} Tm ({text}) Tj ET")
        else:
            ops.append(f"BT /{names[font]} {size} Tf 50 {y} Td ({text}) Tj ET")
    stream = "\n".join(ops).encode("latin-1")
    font_refs = " ".join(f"/{names[font]} {5 + i} 0 R" for i, font in enumerate(fonts))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        f"/Resources << /Font << {font_refs} >> >> >>".encode(),
        f"<< /Length {len(stream)} >> stream\n".encode() + stream + b"\nendstream",
        *[f"<< /Type /Font /Subtype /Type1 /BaseFont /{font} >>".encode() for font in fonts],
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(data))
        data += f"{number} 0 obj ".encode() + body + b" endobj\n"
    xref = len(data)
    data += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    data += "".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode()
    data += f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    data += f"startxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(data))
    return path


def test_the_format_example_gives_font_size_and_headings_in_order(tmp_path: Path) -> None:
    layout = read_format(make_pdf(tmp_path / "format.pdf", FORMAT_LINES))

    assert layout.font == "Arial"  # Helvetica is Arial in Word
    assert layout.body_size == 10.5
    assert layout.name_size == 16
    assert layout.sections == (
        (EDUCATION, "Education"),
        (EXPERIENCE, "Work Experience"),
        (PROJECTS, "Projects"),
        (ACTIVITIES, "Leadership & Activities"),
        (SKILLS, "Technical Skills"),  # not the "Technical Skills: Python, SQL" line
    )
    assert not layout.uppercase
    assert layout.heading_for_kind("leadership") == "Leadership & Activities"
    assert layout.heading_for_kind("project") == "Projects"


def test_sizes_set_by_the_text_matrix_and_unknown_fonts_are_handled(tmp_path: Path) -> None:
    lines = [("CMR10" if font == "Helvetica" else "CMBX10", size, text.upper() if size == 11
              else text) for font, size, text in FORMAT_LINES[:7]]

    layout = read_format(make_pdf(tmp_path / "latex.pdf", lines, unit_size=True))

    assert layout.font == "Times New Roman"  # Computer Modern is not a Word font
    assert (layout.body_size, layout.name_size) == (10.5, 16)
    assert layout.sections == (
        (EDUCATION, "EDUCATION"),
        (EXPERIENCE, "WORK EXPERIENCE"),
        (SKILLS, "Skills"),  # missing from the example, so added at the end
    )
    assert layout.uppercase
    assert layout.heading_for_kind("project") == "WORK EXPERIENCE"  # no Projects section


def test_a_missing_or_unreadable_file_falls_back_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_headings = make_pdf(tmp_path / "plain.pdf", [("Times-Roman", 11, "Just a paragraph")])
    master = make_pdf(tmp_path / "master.pdf", [("Times-Roman", 11, "Hack Club Lead")])
    monkeypatch.setattr(resume_format, "load_dotenv", lambda _path: None)
    monkeypatch.setenv("MASTER_CV_PATH", str(master))
    monkeypatch.setenv("RESUME_FORMAT_PATH", str(no_headings))

    sources = load_resume_sources()

    assert "Hack Club Lead" in sources.master_cv_text
    assert sources.layout == resume_format.DEFAULT_FORMAT
    assert any("No Experience or Projects heading" in note for note in sources.notes)

    monkeypatch.setenv("MASTER_CV_PATH", str(tmp_path / "missing.pdf"))
    monkeypatch.setenv("RESUME_FORMAT_PATH", str(make_pdf(tmp_path / "f.pdf", FORMAT_LINES)))

    sources = load_resume_sources()

    assert sources.master_cv_text == ""
    assert sources.layout.font == "Arial"
    assert sources.notes == ("No master CV at missing.pdf: the resume and library are the only "
                             "sources.",)


class FakeClient:
    """Keywords, then a pool from the master CV, then one tailored draft."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self.create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append(request)
        if request["system"] == KEYWORD_PROMPT:
            payload = {"action_verbs": ["evaluate", "organize"], "technologies": ["Python"],
                       "concepts": [], "required": ["Python"], "preferred": []}
        elif request["system"] == POOL_PROMPT:
            entry = {"organization": "", "location": "", "dates": ""}
            payload = {
                "name": "Jordan Example",
                "contact": ["jordan@example.com", "Raleigh, NC"],
                "education": ["Example State University, B.S. Computer Science, Dec 2025"],
                "entries": [
                    {**entry, "title": "Research Assistant", "organization": "Example Lab",
                     "kind": "experience", "doc_id": "master_cv",
                     "facts": ["Evaluated LLM agents in Python on 40 tasks with an evaluation "
                               "harness."]},
                    {**entry, "title": "Hack Club Lead", "kind": "leadership",
                     "doc_id": "master_cv", "facts": ["Organized a hackathon for 200 students."]},
                    {**entry, "title": "Software Intern", "organization": "Example Co",
                     "kind": "experience", "doc_id": "master_cv",
                     "facts": ["Built a search service in Python used by the support team "
                               "every day."]},
                    {**entry, "title": "Software Intern", "organization": "Example Co",
                     "kind": "experience", "doc_id": "resume",
                     "facts": ["Built a search service in Python used by the support team "
                               "every day."]},
                ],
            }
        else:
            def bullet(text: str, quote: str) -> dict:
                return {"text": text, "evidence": [{"doc_id": "master_cv", "quote": quote}]}

            payload = {
                "sections": [{"heading": "Experience", "entries": [
                    {"entry_id": "e1", "bullets": [bullet(
                        "Evaluated LLM agents in Python on 40 tasks",
                        "Evaluated LLM agents in Python on 40 tasks")]},
                    {"entry_id": "e2", "bullets": [bullet(
                        "Organized a hackathon for 200 students",
                        "Organized a hackathon for 200 students.")]},
                    {"entry_id": "e3", "bullets": [bullet(
                        "Built a search service in Python",
                        "Built a search service in Python")]},
                    {"entry_id": "e4", "bullets": []},  # the same internship again
                ]}],
                "skills": [{"label": "Languages", "items": ["Python"]}],
            }
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(payload))]
        )


def test_entries_come_from_the_master_cv_under_the_formats_headings(tmp_path: Path) -> None:
    layout = read_format(make_pdf(tmp_path / "format.pdf", FORMAT_LINES))
    client = FakeClient()

    resume = tailor_resume(
        JOB, RESUME, client=client, model="m", library_dir=tmp_path / "library",
        master_cv_text=MASTER_CV, layout=layout,
    )

    pool_request = next(r for r in client.requests if r["system"] == POOL_PROMPT)
    sent = json.loads(pool_request["messages"][0]["content"])
    assert [doc["doc_id"] for doc in sent] == ["master_cv", "resume"]
    tailor_request = client.requests[-1]
    assert '"Work Experience", "Projects", "Leadership & Activities"' in tailor_request["system"]
    assert [
        (heading, [entry.title for entry in entries]) for heading, entries in resume.sections
    ] == [
        ("Work Experience", ["Research Assistant", "Software Intern"]),  # listed once
        ("Leadership & Activities", ["Hack Club Lead"]),
    ]
    assert resume.layout is layout


def test_the_word_file_copies_the_formats_look(tmp_path: Path) -> None:
    layout = ResumeFormat(
        font="Arial",
        body_size=10.5,
        name_size=16,
        sections=(
            (EDUCATION, "Education"),
            (EXPERIENCE, "Work Experience"),
            (ACTIVITIES, "Leadership & Activities"),
            (SKILLS, "Technical Skills"),
        ),
        uppercase=False,
    )
    resume = TailoredResume(
        "Jordan Example", ["jordan@example.com"],
        ["Example State University, B.S. Computer Science, Dec 2025"],
        [
            ("Leadership & Activities", [Entry("a", "Hack Club Lead", bullets=["Organized"])]),
            ("Work Experience", [Entry("w", "Software Intern", bullets=["Built search"])]),
        ],
        [("Languages", ["Python"])], Keywords(), layout=layout,
    )

    out = write_docx(resume, tmp_path / "resume.docx")

    paragraphs = Document(str(out)).paragraphs
    runs = [run for paragraph in paragraphs for run in paragraph.runs if run.text]
    assert {run.font.name for run in runs} == {"Arial"}
    assert max(run.font.size.pt for run in runs if not run.bold) == 10.5
    assert runs[0].text == "Jordan Example" and runs[0].font.size.pt == 16
    headings = [p.text for p in paragraphs if p.text in dict(layout.sections).values()]
    assert headings == [
        "Education", "Work Experience", "Leadership & Activities", "Technical Skills"
    ]
    text = [p.text for p in paragraphs]
    assert text.index("Technical Skills") < text.index("Languages: Python")


def test_body_sizes_start_at_the_formats_and_never_go_below_10() -> None:
    assert resume_tailor.font_sizes(ResumeFormat(body_size=11.5)) == [11.5, 11, 10.5, 10]
    assert resume_tailor.font_sizes(ResumeFormat(body_size=10)) == [10]
