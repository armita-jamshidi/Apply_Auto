"""Shared post-submit confirmation detection for live application attempts."""

import re

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

CONFIRMATION_PATTERN = re.compile(
    r"application (?:was |has been )?(?:successfully )?(?:submitted|received)"
    r"|thank(?:s| you) for (?:applying|your application|submitting)",
    re.IGNORECASE,
)
CONFIRMATION_TIMEOUT_MS = 30000
UNKNOWN_OUTCOME_ERROR = (
    "Submit was clicked but no confirmation was detected; verify manually on the employer "
    "site or by email before retrying."
)


def confirmation_visible(page: Page) -> bool:
    """Return whether confirmation-like text is already on the page."""
    return page.get_by_text(CONFIRMATION_PATTERN).count() > 0


def wait_for_confirmation(page: Page) -> bool:
    """Wait for a submission confirmation message; False when none appears."""
    try:
        page.get_by_text(CONFIRMATION_PATTERN).first.wait_for(
            state="visible", timeout=CONFIRMATION_TIMEOUT_MS
        )
    except PlaywrightError:
        return False
    return True
