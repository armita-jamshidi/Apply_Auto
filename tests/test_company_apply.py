"""Tests for finding a job's application on the company's own site."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent import dashboard, pipeline
from agent.sources.company_apply import (
    BoardCache,
    find_company_application,
    links_in,
    resolve_listing,
)
from agent.types import JobListing
from db.models import Base, Job


def board_job(platform: str, company: str, title: str, url: str) -> JobListing:
    return JobListing(platform, platform, company, title, url, "Remote - US", "Board text.")


def fake_cache(boards: dict[tuple[str, str], list[JobListing]], calls: list | None = None):
    def fetcher(platform: str):
        def fetch(board: str, _company: str, **_kwargs) -> list[JobListing]:
            if calls is not None:
                calls.append((platform, board))
            if (platform, board) not in boards:
                raise RuntimeError("404")
            return boards[(platform, board)]

        return fetch

    return BoardCache({name: fetcher(name) for name in ("greenhouse", "lever", "ashby")})


def wwr(title: str, company: str, description: str = "", links: tuple[str, ...] = ()):
    return JobListing(
        "weworkremotely", "weworkremotely", company, title,
        "https://weworkremotely.com/remote-jobs/x", "Remote - US", description, links,
    )


def test_ats_job_link_in_the_posting_is_used_directly() -> None:
    found = find_company_application(
        "Acme",
        "AI Engineer",
        ["https://acme.com", "https://jobs.lever.co/acme/1234-abcd/apply?source=wwr"],
        fake_cache({}),
    )

    assert (found.platform, found.url) == ("lever", "https://jobs.lever.co/acme/1234-abcd")


def test_greenhouse_embed_and_gh_jid_links_become_board_jobs() -> None:
    found = find_company_application(
        "Acme",
        "AI Engineer",
        ["https://boards.greenhouse.io/embed/job_app?for=acme&token=4242"],
        fake_cache({}),
    )

    assert found.url == "https://job-boards.greenhouse.io/acme/jobs/4242"


def test_company_board_is_found_by_name_and_matched_by_title() -> None:
    target = board_job("ashby", "Tether", "AI Harness Engineer", "https://jobs.ashbyhq.com/tether/1")
    other = board_job("ashby", "Tether", "Designer", "https://jobs.ashbyhq.com/tether/2")
    calls: list = []
    cache = fake_cache({("ashby", "tether"): [other, target]}, calls)

    listing = resolve_listing(wwr("AI Harness Engineer", "Tether Inc."), cache)

    assert (listing.platform, listing.url) == ("ashby", "https://jobs.ashbyhq.com/tether/1")
    assert listing.source == "weworkremotely"
    assert listing.description == "Board text."
    resolve_listing(wwr("AI Harness Engineer", "Tether Inc."), cache)
    assert calls.count(("ashby", "tether")) == 1


def test_company_website_names_the_board() -> None:
    target = board_job("greenhouse", "Radar", "ML Engineer", "https://job-boards.greenhouse.io/radarlabs/jobs/7")
    cache = fake_cache({("greenhouse", "radarlabs"): [target]})

    listing = resolve_listing(
        wwr("ML Engineer", "Radar", description='<a href="https://www.radarlabs.com">site</a>'),
        cache,
    )

    assert listing.url == "https://job-boards.greenhouse.io/radarlabs/jobs/7"


def test_careers_page_is_kept_when_no_board_job_matches() -> None:
    listing = resolve_listing(
        wwr(
            "AI Engineer",
            "Spade",
            links=("https://news.ycombinator.com/item?id=1", "https://spade.com/careers#ai"),
        ),
        fake_cache({}),
    )

    assert listing.platform == "weworkremotely"
    assert listing.apply_url == "https://spade.com/careers#ai"


def test_nothing_better_leaves_the_posting() -> None:
    listing = resolve_listing(wwr("AI Engineer", "Quiet Co"), fake_cache({}))

    assert listing.apply_url is None and listing.platform == "weworkremotely"


def test_links_in_reads_hrefs_and_bare_urls() -> None:
    text = '<a href="https://a.com/jobs?x=1&amp;y=2">apply</a> or see https://b.com/careers.'

    assert links_in(text) == ["https://a.com/jobs?x=1&y=2", "https://b.com/careers"]


def test_job_run_links_existing_jobs_and_dashboard_shows_apply_link(tmp_path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = board_job("lever", "Tether", "AI Harness Engineer", "https://jobs.lever.co/tether/9")
    with Session(engine) as session:
        def add(title: str, company: str, url: str, platform: str = "hackernews", **kw) -> Job:
            job = Job(
                source=platform, platform=platform, company=company, title=title, url=url,
                location_raw="Remote - US", location_category="remote_us",
                description=kw.pop("description", "Post."), status="new", **kw,
            )
            session.add(job)
            session.flush()
            return job

        hn = add("AI Harness Engineer", "Tether", "https://news.ycombinator.com/item?id=1")
        careers = add(
            "AI Engineer", "Spade", "https://news.ycombinator.com/item?id=2",
            description="Apply at https://spade.com/careers",
        )
        none = add("AI Engineer", "Quiet Co", "https://news.ycombinator.com/item?id=3")
        ashby = add("Agent Engineer", "Acme", "https://jobs.ashbyhq.com/acme/1", platform="ashby")

        found = pipeline.resolve_application_links(
            session, fake_cache({("lever", "tether"): [target]})
        )

        assert found == 2
        assert (hn.platform, hn.url) == ("lever", "https://jobs.lever.co/tether/9")
        assert careers.apply_url == "https://spade.com/careers"
        assert none.apply_url == none.url

        page = tmp_path / "dashboard.html"
        dashboard.write_dashboard(session, page, fit_threshold=70)
        html = page.read_text(encoding="utf-8")
        assert "href='https://jobs.lever.co/tether/9/apply' class='apply-link'" in html
        # Only the general careers page is known: it is labelled as such, and the form
        # agent is not offered for it.
        assert (
            "href='https://spade.com/careers' class='apply-link' target='_blank' "
            "rel='noopener'>Careers page"
        ) in html
        assert "Role page not looked up yet" in html
        assert f"href='{ashby.url}/application' class='apply-link'" in html
        assert "--job-url &quot;https://spade.com/careers&quot;" not in html
    engine.dispose()
