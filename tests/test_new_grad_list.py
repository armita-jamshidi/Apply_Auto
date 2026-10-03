"""Tests for finding company boards from the public new-grad list (offline fixtures)."""

import httpx
import pytest

from agent.settings import CompanyConfig
from agent.sources.new_grad_list import (
    board_from_url,
    companies_from_list,
    fetch_new_grad_companies,
)

LIST = """
<table><tbody>
<tr>
<td><strong><a href="https://simplify.jobs/c/Parasail">Parasail</a></strong></td>
<td>Software Engineer New Grad</td><td>San Mateo, CA</td>
<td><a href="https://jobs.ashbyhq.com/parasail/da59/application?embed=true&amp;utm_source=Simplify">
<img alt="Apply"></a> <a href="https://simplify.jobs/p/1b4b"><img alt="Simplify"></a></td>
<td>0d</td>
</tr>
<tr>
<td>\u21b3</td><td>Data Engineer New Grad</td><td>Remote in USA</td>
<td><a href="https://jobs.ashbyhq.com/parasail/other/application"><img alt="Apply"></a></td>
<td>1d</td>
</tr>
<tr>
<td><strong>TripAdvisor</strong></td><td>Software Engineer I</td><td>Needham, MA</td>
<td><a href="https://job-boards.greenhouse.io/tripadvisor/jobs/6903058?utm_source=Simplify">
Apply</a></td><td>2d</td>
</tr>
<tr>
<td><strong>Big Co</strong></td><td>Engineer</td><td>NYC</td>
<td><a href="https://bigco.wd5.myworkdayjobs.com/en-US/careers/job/123">Apply</a></td><td>3d</td>
</tr>
<tr>
<td><strong>Steerbridge</strong></td><td>Analyst</td><td>Remote</td>
<td><a href="https://jobs.lever.co/steerbridge/718b/apply">Apply</a></td><td>4d</td>
</tr>
</tbody></table>
"""


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://job-boards.greenhouse.io/tripadvisor/jobs/1?x=1", ("greenhouse", "tripadvisor")),
        ("https://boards.greenhouse.io/parallel/jobs/5", ("greenhouse", "parallel")),
        ("https://boards.greenhouse.io/embed/job_app?for=acme&token=9", ("greenhouse", "acme")),
        ("https://jobs.lever.co/palantir/d372/apply", ("lever", "palantir")),
        ("https://jobs.ashbyhq.com/harvey/b099/application", ("ashby", "harvey")),
        ("https://jobs.smartrecruiters.com/Visa/7440", ("smartrecruiters", "Visa")),
        (
            "https://jobs.smartrecruiters.com/oneclick-ui/company/Experian/publication/90ce",
            ("smartrecruiters", "Experian"),
        ),
        ("https://nvidia.wd5.myworkdayjobs.com/careers/job/1", None),
        ("https://simplify.jobs/p/1b4b", None),
        ("https://jobs.lever.co/", None),
    ],
)
def test_board_from_url(url: str, expected: tuple[str, str] | None) -> None:
    assert board_from_url(url) == expected


def test_companies_from_list_dedupes_boards_and_follows_continuation_rows() -> None:
    companies = companies_from_list(LIST)

    assert [(c.name, c.platform, c.board) for c in companies] == [
        ("Parasail", "ashby", "parasail"),
        ("TripAdvisor", "greenhouse", "tripadvisor"),
        ("Steerbridge", "lever", "steerbridge"),
    ]


def test_fetch_new_grad_companies_skips_configured_boards() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=LIST))
    client = httpx.Client(transport=transport)
    configured = [CompanyConfig("Trip", "greenhouse", "TripAdvisor", None)]

    companies = fetch_new_grad_companies(exclude=configured, client=client)
    client.close()

    assert [c.board for c in companies] == ["parasail", "steerbridge"]
