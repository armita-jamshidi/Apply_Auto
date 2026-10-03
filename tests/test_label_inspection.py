"""Run the label-inspection script in a real browser against small, offline form snippets."""

from collections.abc import Iterator

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from agent.applier.greenhouse import LabelInfo, inspect_label

FORM_HTML = """
<form>
  <label>Current location <span>&#x2731;</span>
    <input type="text" name="location">
    <div style="display: none">No location found. Try entering a different location</div>
  </label>

  <label>Veteran status
    <select name="veteran"><option>Select ...</option><option>I am a veteran</option></select>
  </label>

  <li class="application-question">
    <div class="application-label"><div class="text">Language Skill(s)</div></div>
    <ul>
      <li><label><input type="checkbox" name="lang-1">English (ENG)</label></li>
      <li><label><input type="checkbox" name="lang-2">Spanish (SPA)</label></li>
    </ul>
  </li>

  <fieldset>
    <label>Race</label>
    <div><label><input type="radio" name="uuid__race" value="a">Asian</label></div>
    <div><label><input type="radio" name="uuid__race" value="b">Hispanic or Latino</label></div>
  </fieldset>

  <div><label>When can you start a new role?</label><button type="button">Pick a date</button></div>
</form>
"""


@pytest.fixture(scope="module")
def page() -> Iterator[Page]:
    try:
        playwright = sync_playwright().start()
    except PlaywrightError as error:
        pytest.skip(f"Playwright is unavailable: {error}")
    try:
        browser = playwright.chromium.launch(headless=True)
    except PlaywrightError as error:
        playwright.stop()
        pytest.skip(f"Chromium is not installed: {error}")
    form_page = browser.new_page()
    form_page.set_content(FORM_HTML)
    yield form_page
    browser.close()
    playwright.stop()


def labels(page: Page) -> list[LabelInfo]:
    locator = page.locator("label")
    return [inspect_label(locator.nth(index)) for index in range(locator.count())]


def test_label_text_skips_hidden_errors_and_nested_options(page: Page) -> None:
    location, veteran, *_ = labels(page)

    assert location == LabelInfo("Current location ✱", "text", None)
    assert veteran == LabelInfo("Veteran status", "select-one", None)


def test_lever_checkbox_options_share_the_question_heading(page: Page) -> None:
    infos = labels(page)

    assert infos[2] == LabelInfo("English (ENG)", "checkbox", "Language Skill(s)")
    assert infos[3] == LabelInfo("Spanish (SPA)", "checkbox", "Language Skill(s)")


def test_ashby_fieldset_heading_and_radio_options_form_one_group(page: Page) -> None:
    infos = labels(page)

    assert infos[4] == LabelInfo("Race", "choice-group", "Race")
    assert infos[5] == LabelInfo("Asian", "radio", "Race")
    assert infos[6] == LabelInfo("Hispanic or Latino", "radio", "Race")


def test_label_without_choices_or_control_is_a_custom_widget(page: Page) -> None:
    assert labels(page)[7] == LabelInfo("When can you start a new role?", None, None)
