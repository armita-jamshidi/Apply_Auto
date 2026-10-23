"""Deterministic job location filtering and duplicate persistence."""

import re

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from agent.fetchers.greenhouse import greenhouse_job_id
from agent.seniority import classify_experience
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


def mentions_north_carolina(text: str) -> bool:
    """Return whether text names North Carolina or one of its main cities."""
    return _contains_nc(text)


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
    """Return whether a location label is too vague for automatic decisions.

    A bare country label ("United States") without "remote" could be on-site anywhere.
    """
    country_only = re.sub(
        r"\b(?:u\.?s\.?a?\.?|united states(?: of america)?)(?!\w)|[^\w]+",
        "",
        location,
        flags=re.IGNORECASE,
    )
    if location.strip() and not country_only:
        return True
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


HUMAN_LANGUAGES = (
    "Spanish", "French", "German", "Portuguese", "Italian", "Mandarin", "Chinese", "Cantonese",
    "Japanese", "Korean", "Arabic", "Hindi", "Russian", "Dutch", "Hebrew", "Turkish",
    "Vietnamese", "Polish", "Farsi", "Persian", "Tagalog", "Urdu", "Bengali", "Punjabi", "Greek",
    "Swedish", "Norwegian", "Danish", "Finnish", "Thai", "Indonesian", "Ukrainian", "Romanian",
    "Czech", "Hungarian", "Swahili", "American Sign Language",
)
_LANGUAGE_REQUIRED = re.compile(
    r"\b(?:fluen\w*|proficien\w*|bilingual|native|business[- ]level|working knowledge"
    r"|required|must|(?:ability to|you) (?:speak|read|write))\b",
    re.IGNORECASE,
)
_LANGUAGE_OPTIONAL = re.compile(
    r"\b(?:a plus|plus|preferred|nice to have|bonus|advantage|desirable|beneficial)\b",
    re.IGNORECASE,
)


def find_language_dealbreakers(description: str, spoken_languages: list[str]) -> list[str]:
    """Find human languages a job requires that the candidate does not list."""
    spoken = {
        name.strip().casefold()
        for language in spoken_languages
        for name in str(language).split("/")
        if name.strip()
    }
    missing: list[str] = []
    for sentence in re.split(r"(?<=[.!?;\n])\s*", description):
        if not _LANGUAGE_REQUIRED.search(sentence) or _LANGUAGE_OPTIONAL.search(sentence):
            continue
        for language in HUMAN_LANGUAGES:
            if language.casefold() in spoken or language in missing:
                continue
            if re.search(rf"\b{re.escape(language)}\b", sentence):
                missing.append(language)
    return [
        f"Requires {language} proficiency, which the profile does not list" for language in missing
    ]


def normalize_location(location: str, *, include_hybrid_nc: bool = True) -> LocationCategory:
    """Classify a listing as US-remote, North Carolina, or out of scope."""
    text = " ".join(location.split())
    if not text:
        return "other"

    remote = bool(re.search(r"\b(?:remote|anywhere)\b", text, flags=re.IGNORECASE))
    hybrid = bool(re.search(r"\bhybrid\b", text, flags=re.IGNORECASE))
    is_nc = _contains_nc(text)

    if hybrid:
        return "nc" if include_hybrid_nc and is_nc else "other"
    if remote:
        if _contains_other_remote_restriction(text):
            return "other"
        if is_nc:
            return "nc"
        return "remote_us" if _is_us_wide_remote(text) else "other"
    return "nc" if is_nc else "other"


def _same_greenhouse_job(existing: Job, listing: JobListing) -> bool:
    """Whether a stored Greenhouse job is this listing's job, with links that differ."""
    if existing.platform != "greenhouse" or listing.platform != "greenhouse":
        return False
    if (existing.url, existing.apply_url) == (listing.url, listing.apply_url):
        return False
    job_id = greenhouse_job_id(listing.url)
    return job_id is not None and greenhouse_job_id(existing.url) == job_id


def persist_job_if_new(
    session: Session,
    listing: JobListing,
    location_category: LocationCategory,
    *,
    status: str = "new",
) -> bool:
    """Persist a listing unless the same job is already stored; return whether it was added.

    The same job is one with the same URL, or the same title at the same company: a role
    seen on several boards, or a board job whose stored link was later changed to the
    company's own application. A stored job missing its posting date gets this one's.
    """
    existing = session.scalar(select(Job).where(Job.url == listing.url)) or same_role(
        session, listing.company, listing.title
    )
    if existing is not None:
        if existing.posted_at is None and listing.posted_at is not None:
            existing.posted_at = listing.posted_at
        if _same_greenhouse_job(existing, listing):
            # Stored before company-hosted Greenhouse pages were split from the board link,
            # or with a company page that only lists openings: use the current links.
            existing.url = listing.url
            existing.apply_url = listing.apply_url
        return False
    experience = classify_experience(listing.title, listing.description)

    session.add(
        Job(
            source=listing.source,
            platform=listing.platform,
            company=listing.company,
            title=listing.title,
            url=listing.url,
            apply_url=listing.apply_url,
            location_raw=listing.location_raw,
            location_category=location_category,
            description=listing.description,
            posted_at=listing.posted_at,
            status=status,
            experience_level=experience.level,
            min_years_experience=experience.min_years,
        )
    )
    session.flush()
    return True


def same_role(session: Session, company: str, title: str) -> Job | None:
    """A stored job with the same title at the same company, ignoring case and punctuation.

    The title may be the stored one or, for a job retitled from the company's own site, the
    title the third-party site used.
    """
    wanted_company = company_key(company)
    wanted_title = _words(title)
    # Only the first character narrows the query; escape it in case it is a LIKE wildcard.
    first = title.strip()[:1].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    candidates = select(Job).where(
        or_(
            Job.title.ilike(first + "%", escape="\\"),
            Job.listed_title.ilike(first + "%", escape="\\"),
        )
    )
    for job in session.scalars(candidates):
        titles = {_words(job.title), _words(job.listed_title or "")}
        if wanted_title in titles and company_key(job.company) == wanted_company:
            return job
    return None


def company_key(company: str) -> str:
    """A company name without case, punctuation, or suffixes such as Inc. or LLC."""
    name = re.sub(
        r"\b(?:inc|incorporated|llc|ltd|limited|corp|corporation|co|company|plc|gmbh)\b\.?",
        " ",
        company.casefold(),
    )
    return _words(name)


def _words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


# Countries and main cities outside the US, for "headquartered in Berlin" or "a London-based
# startup". A company based in one of these is kept only when the posting names a US office.
_FOREIGN_PLACES = (
    "United Kingdom|UK|U\\.K\\.|England|Scotland|Wales|London|Manchester|Cambridge, UK|"
    "Edinburgh|Ireland|Dublin|Germany|Berlin|Munich|Hamburg|France|Paris|Netherlands|"
    "Amsterdam|Belgium|Brussels|Spain|Madrid|Barcelona|Portugal|Lisbon|Italy|Milan|Rome|"
    "Switzerland|Zurich|Zürich|Geneva|Austria|Vienna|Sweden|Stockholm|Norway|Oslo|Denmark|"
    "Copenhagen|Finland|Helsinki|Estonia|Tallinn|Poland|Warsaw|Krakow|Czech Republic|Prague|"
    "Romania|Bucharest|Ukraine|Kyiv|Greece|Athens|Turkey|Istanbul|Israel|Tel Aviv|Jerusalem|"
    "United Arab Emirates|UAE|Dubai|Abu Dhabi|India|Bangalore|Bengaluru|Mumbai|Delhi|"
    "Hyderabad|Pune|Chennai|Pakistan|Singapore|Japan|Tokyo|South Korea|Korea|Seoul|China|"
    "Beijing|Shanghai|Shenzhen|Hong Kong|Taiwan|Taipei|Vietnam|Philippines|Manila|Indonesia|"
    "Jakarta|Australia|Sydney|Melbourne|New Zealand|Auckland|Canada|Toronto|Vancouver|"
    "Montreal|Montréal|Ottawa|Calgary|Waterloo|Mexico|Mexico City|Brazil|São Paulo|Sao Paulo|"
    "Argentina|Buenos Aires|Colombia|Bogotá|Chile|Santiago|Nigeria|Lagos|Kenya|Nairobi|"
    "South Africa|Cape Town|Egypt|Cairo|Europe|the EU"
)
_FOREIGN_HQ = re.compile(
    rf"(?i:\b(?:headquartered|headquarters|based|hq|head office))\s*(?i:is\s+|are\s+)?"
    rf"(?i:in|out of|:)\s+(?i:the\s+)?(?:{_FOREIGN_PLACES})(?!\w)"
    rf"|(?<!\w)(?:{_FOREIGN_PLACES})[- ](?i:based|headquartered)\b",
)
_US_PLACE = (
    rf"(?:US|U\.S\.A?\.?|USA|United States|{_US_STATES}|New York City|NYC|San Francisco|"
    r"SF|Bay Area|Silicon Valley|Los Angeles|Boston|Seattle|Austin|Chicago|Denver|Atlanta|"
    r"Miami|Philadelphia|Pittsburgh|Nashville|Salt Lake City|Washington,? D\.?C\.?|"
    r"Palo Alto|Mountain View|Menlo Park|Cambridge, MA|Raleigh|Durham|Chapel Hill|Cary|"
    r"Morrisville|Charlotte|Research Triangle)(?!\w)"
)
_OFFICE_WORDS = r"(?i:offices?|hubs?|entity|subsidiary|headquarters|hq|locations?)"
# "offices in London and New York", "a hub in Boston", or "our US office", "a U.S. entity".
_US_OFFICE = re.compile(
    rf"\b{_OFFICE_WORDS}\s+(?:\w+\s+){{0,3}}?(?i:in|across)\s+(?i:the\s+)?"
    rf"(?:[A-Z][\w.'-]*(?:\s[A-Z][\w.'-]*)*(?:,\s*|\s+(?:and|&)\s+))*{_US_PLACE}"
    rf"|(?<!\w){_US_PLACE}(?:[- ]based)?\s+{_OFFICE_WORDS}\b"
)


def non_us_company_reason(description: str, location_category: str) -> str | None:
    """Why a job is at a company based outside the US with no US office, or None.

    A role in North Carolina is at a US office already. Otherwise a posting that says the
    company is based abroad ("headquartered in London", "a Berlin-based startup") is ruled
    out unless a sentence names a US office ("offices in London and New York"). A posting
    that says nothing about where the company is based is kept.
    """
    if location_category == "nc":
        return None
    text = re.sub(r"<[^>]+>", "\n", description)
    # "U.S." and "D.C." do not end a sentence.
    sentences = re.split(r"(?<=[a-z0-9)][.!?;])\s+|\n+", text)
    if not any(_FOREIGN_HQ.search(" ".join(s.split())) for s in sentences):
        return None
    if any(_US_OFFICE.search(" ".join(s.split())) for s in sentences):
        return None
    return "Company is based outside the US with no US office"
