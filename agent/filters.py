"""Deterministic job location filtering and duplicate persistence."""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Job

from .types import JobListing, LocationCategory

_NC_CITIES = (
    "Raleigh",
    "Durham",
    "Chapel Hill",
    "Cary",
    "Morrisville",
    "Charlotte",
    "Greensboro",
    "Winston-Salem",
    "Wilmington",
)
_OTHER_REMOTE_REGIONS = (
    "EMEA",
    "Europe",
    "European Union",
    "United Kingdom",
    "UK",
    "Canada",
    "Mexico",
    "India",
    "Australia",
    "APAC",
    "LATAM",
)
_US_STATES = (
    "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|"
    "Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|"
    "Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|"
    "New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|"
    "Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|"
    "West Virginia|Wisconsin|Wyoming"
)
_STATE_CODES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM "
    "NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY"
).split()


def _contains_nc(text: str) -> bool:
    if re.search(r"\b(?:north carolina|nc)\b", text, flags=re.IGNORECASE):
        return True
    if re.search(r"\bresearch triangle park\b|\brtc\b", text, flags=re.IGNORECASE):
        return True
    if re.search(r"\brtp\b", text, flags=re.IGNORECASE):
        return True
    return any(
        re.search(rf"\b{re.escape(city)}\b", text, flags=re.IGNORECASE) for city in _NC_CITIES
    )


def _contains_other_remote_restriction(text: str) -> bool:
    for region in _OTHER_REMOTE_REGIONS:
        if re.search(rf"\b{re.escape(region)}\b", text, flags=re.IGNORECASE):
            return True
    text_without_nc = re.sub(r"\b(?:north carolina|nc)\b", " ", text, flags=re.IGNORECASE)
    if re.search(rf"\b(?:{_US_STATES})\b", text_without_nc, flags=re.IGNORECASE):
        return True
    state_codes = "|".join(code for code in _STATE_CODES if code != "NC")
    return bool(re.search(rf"\b(?:{state_codes})\b", text))


def _is_us_wide_remote(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:u\.?s\.?a?\.?|united states(?: of america)?)\b|"
            r"\banywhere in (?:the )?(?:u\.?s\.?a?\.?|united states)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def is_ambiguous_location(location: str) -> bool:
    """Return whether a location label is too vague for automatic decisions."""
    return bool(
        re.search(
            r"\b(?:multiple|various) locations\b|\blocation(?:s)?\s+(?:are\s+)?flexible\b|"
            r"\bflexible\b",
            location,
            flags=re.IGNORECASE,
        )
    )


def find_non_nc_workplace_dealbreakers(description: str) -> list[str]:
    """Find explicitly named non-NC states tied to hybrid or office requirements."""
    work_mode = re.compile(r"\b(?:hybrid|on[- ]site|in[- ]office|office[- ]based)\b", re.IGNORECASE)
    state_names = re.compile(
        rf"\b(?:{_US_STATES.replace('|North Carolina', '').replace('North Carolina|', '')})\b",
        re.IGNORECASE,
    )
    state_codes = "|".join(code for code in _STATE_CODES if code != "NC")
    found: list[str] = []

    for sentence in re.split(r"(?<=[.!?;\n])\s*", description):
        if not work_mode.search(sentence):
            continue
        locations = [match.group(0) for match in state_names.finditer(sentence)]
        locations.extend(re.findall(rf"\b(?:{state_codes})\b", sentence))
        for location in locations:
            label = location.upper() if len(location) == 2 else location
            if label not in found:
                found.append(label)

    if not found:
        return []
    return [
        "Requires hybrid or in-office work outside North Carolina: " + ", ".join(found)
    ]


def normalize_location(location: str, *, include_hybrid_nc: bool = True) -> LocationCategory:
    """Classify a listing as US-remote, North Carolina, or out of scope."""
    text = " ".join(location.split())
    if not text:
        return "other"

    remote = bool(re.search(r"\bremote\b", text, flags=re.IGNORECASE))
    hybrid = bool(re.search(r"\bhybrid\b", text, flags=re.IGNORECASE))
    is_nc = _contains_nc(text)

    if hybrid:
        return "nc" if include_hybrid_nc and is_nc else "other"
    if remote or _is_us_wide_remote(text):
        if _contains_other_remote_restriction(text):
            return "other"
        if is_nc:
            return "nc"
        return "remote_us" if _is_us_wide_remote(text) else "other"
    return "nc" if is_nc else "other"


def persist_job_if_new(
    session: Session,
    listing: JobListing,
    location_category: LocationCategory,
    *,
    status: str = "new",
) -> bool:
    """Persist a listing unless a job with its URL is already present."""
    exists = session.scalar(select(Job.id).where(Job.url == listing.url))
    if exists is not None:
        return False

    session.add(
        Job(
            source=listing.source,
            platform=listing.platform,
            company=listing.company,
            title=listing.title,
            url=listing.url,
            location_raw=listing.location_raw,
            location_category=location_category,
            description=listing.description,
            status=status,
        )
    )
    session.flush()
    return True
