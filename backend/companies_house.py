"""
Companies House Public Data API — last-resort owner lookup.

Used only when website scraping (contact_enricher.py) found no owner/partner
name. Many small local businesses (sole traders, unregistered partnerships)
won't have a Companies House record at all — that's expected, not an error;
this module just returns None and the CSV row keeps whatever it already had.

Beyond bare company_status == "active", a candidate is also rejected if its
status is in an explicit dead/winding-down set (dissolved, liquidation,
active-proposal-to-strike-off, etc.), if its accounts are overdue, or if
filing-history has no filing dated in the current calendar year — quiet on
paper is treated like dissolved for lead quality. When accepted, the accounts'
last-filed date is surfaced in the CSV's "Owner Evidence" column so the user
can judge freshness at a glance.

Auth: HTTP Basic auth, API key as username, blank password.
Docs: https://developer-specs.company-information.service.gov.uk

Rate limiting: Companies House allows 600 requests / 5 minutes and will ban
apps that "regularly exceed or attempt to bypass" that limit. We stay well
under it with a hard-capped sliding-window throttle (see config.py), plus a
one-shot backoff-and-retry if a 429 slips through anyway.
"""

import difflib
import re
import threading
import time
from collections import deque
from datetime import date
from typing import Any, Dict, List, Optional, Set

import requests
from requests.auth import HTTPBasicAuth

from config import (
    COMPANIES_HOUSE_API_KEY,
    COMPANIES_HOUSE_REQUEST_TIMEOUT_SECONDS,
    COMPANIES_HOUSE_SAFE_REQUEST_LIMIT,
    COMPANIES_HOUSE_WINDOW_SECONDS,
)

BASE_URL = "https://api.company-information.service.gov.uk"

_session = requests.Session()

# --- Sliding-window rate limiter, shared across all enrichment threads ---
_rl_lock = threading.Lock()
_rl_timestamps: deque = deque()


def _throttle():
    """Block the calling thread until it's safe to make another request
    without risking the 600/5min cap. Shared across the whole thread pool."""
    while True:
        with _rl_lock:
            now = time.monotonic()
            while _rl_timestamps and now - _rl_timestamps[0] > COMPANIES_HOUSE_WINDOW_SECONDS:
                _rl_timestamps.popleft()
            if len(_rl_timestamps) < COMPANIES_HOUSE_SAFE_REQUEST_LIMIT:
                _rl_timestamps.append(now)
                return
            sleep_for = COMPANIES_HOUSE_WINDOW_SECONDS - (now - _rl_timestamps[0]) + 0.1
        time.sleep(max(sleep_for, 0.1))


def _get(path: str, params: Optional[Dict] = None) -> Optional[Dict]:
    if not COMPANIES_HOUSE_API_KEY:
        return None
    _throttle()
    auth = HTTPBasicAuth(COMPANIES_HOUSE_API_KEY, "")
    try:
        resp = _session.get(
            BASE_URL + path, params=params, auth=auth,
            timeout=COMPANIES_HOUSE_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        return None

    if resp.status_code == 429:
        # Shouldn't happen given the throttle above, but respect it if it does
        # rather than hammering a rate-limited endpoint.
        retry_after = resp.headers.get("Retry-After")
        wait = float(retry_after) if retry_after else COMPANIES_HOUSE_WINDOW_SECONDS
        time.sleep(min(wait, COMPANIES_HOUSE_WINDOW_SECONDS) + 1)
        try:
            resp = _session.get(
                BASE_URL + path, params=params, auth=auth,
                timeout=COMPANIES_HOUSE_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            return None

    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


_SUFFIX_RE = re.compile(r"\b(ltd|limited|plc|llp|llc|inc)\b", re.I)


def _normalize_name(name: str) -> str:
    name = _SUFFIX_RE.sub("", name or "")
    name = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    return re.sub(r"\s+", " ", name).strip()


def _name_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _normalize_name(a), _normalize_name(b)).ratio()


# Plain character-similarity is easily fooled by shared *category* words —
# "Kebabish Grill & Steakhouse" vs "Wild West Grill & Steakhouse" scores high
# on overlap despite being unrelated businesses. These are stripped out before
# comparing, so matching hinges on the actual brand/name words instead.
GENERIC_NAME_TOKENS = {
    "the", "and", "of", "a", "an", "at", "on", "in", "for", "n",
    "ltd", "limited", "plc", "llp", "llc", "inc", "co", "company", "group", "holdings",
    "restaurant", "restaurants", "cafe", "cafes", "coffee", "bar", "bars", "pub", "pubs",
    "grill", "grills", "steakhouse", "kitchen", "kitchens", "house", "houses", "rooms", "room",
    "centre", "center", "centres", "gym", "gyms", "fitness", "sports", "salon", "salons",
    "hair", "hairdressing", "hairdressers", "barber", "barbers", "barbershop", "grooming",
    "beauty", "studio", "therapies", "holistic", "holistics",
    "hotel", "hotels", "inn", "inns", "guest", "guesthouse", "bnb",
    "takeaway", "takeaways", "pizza", "pizzas", "sweet", "sweets", "banquet", "banqueting",
    "bistro", "bistrot", "daily", "stores", "store", "shop", "shops", "express", "uk", "gb",
    "tea", "tapas", "indian", "italian", "italia", "chinese", "thai", "english",
    "club", "clubs", "social",
}


def _name_tokens(name: str) -> list:
    name = (name or "").lower()
    name = re.sub(r"'s\b", "", name)          # possessive: Emilio's -> Emilio
    name = re.sub(r"[^a-z0-9 ]", "", name)    # drop punctuation without splitting words
    return [t for t in name.split() if t]


def _distinctive_tokens(name: str) -> list:
    return [t for t in _name_tokens(name) if t not in GENERIC_NAME_TOKENS]


def _token_match(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) <= 2 or len(b) <= 2:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def _plausible_match(business_name: str, company_title: str) -> bool:
    """Hard gate before a director's name ever gets attached to a business.
    Requires the business's own distinctive (non-category) words to actually
    show up in the company's registered title — a wrong name on the wrong
    business is worse than leaving the field blank."""
    biz_tokens = _distinctive_tokens(business_name)
    company_tokens = _distinctive_tokens(company_title)
    if not biz_tokens:
        return False  # nothing to anchor the match on — too risky to guess

    remaining = list[Any](company_tokens)
    matched = 0
    for bt in biz_tokens:
        hit = next((ct for ct in remaining if _token_match(bt, ct)), None)
        if hit:
            remaining.remove(hit)
            matched += 1

    if len(biz_tokens) <= 2:
        if matched == len(biz_tokens):
            return True
    elif matched / len(biz_tokens) >= 0.65 and matched >= len(biz_tokens) - 1:
        return True

    # Spacing-only differences ("333 Take Away" vs "333 Takeaway") won't
    # line up token-for-token — collapse and compare as a last check.
    joined_biz, joined_company = "".join(biz_tokens), "".join(company_tokens)
    if joined_biz and (
        joined_biz == joined_company
        or difflib.SequenceMatcher(None, joined_biz, joined_company).ratio() >= 0.9
    ):
        return True
    return False


# Name matching alone isn't enough — "Arcane Bar" vs "Arcane AI Limited" or
# "Domino's" vs "Domino Sales Limited" pass the token gate above but are
# unrelated companies that happen to share a word. Cross-checking the
# candidate's registered SIC (business activity) code against the category
# the listing was found under catches these: an IT company's SIC won't match
# "bar". Prefixes are matched against the first 4 (or 2, for the broad
# "shop" bucket) digits of each 5-digit UK SIC code.
CATEGORY_SIC_PREFIXES = {
    "cafe": ("5610",),
    "restaurant": ("5610",),
    "fast_food": ("5610",),
    "pub": ("5630",),
    "bar": ("5630",),
    "club": ("5630", "9311", "9312", "9499"),
    "bnb": ("5520",),
    "hotel": ("5510",),
    "gym": ("9311", "9313"),
    "salon": ("9602",),
    "shop": ("47",),
}


def _sic_matches_category(sic_codes: list, category: str) -> bool:
    prefixes = CATEGORY_SIC_PREFIXES.get(category)
    if not prefixes:
        return True  # no mapping for this category — don't block on it
    return any(code.startswith(p) for code in sic_codes or [] for p in prefixes)


def _get_profile(company_number: str) -> Optional[Dict]:
    return _get(f"/company/{company_number}")


# Exact company_status values from the Companies House API that must never
# produce an owner/address hit — treated the same as "not a live lead".
REJECT_STATUSES: Set[str] = {
    "dissolved",
    "liquidation",
    "receivership",
    "administration",
    "insolvency-proceedings",
    "voluntary-arrangement",
    "converted-closed",
    "closed",
    "active-proposal-to-strike-off",
}


def status_acceptable(status: Optional[str]) -> bool:
    """True only for a plain active company (not proposal-to-strike-off, etc.)."""
    if not status or status in REJECT_STATUSES:
        return False
    return status == "active"


def has_filing_in_year(items: List[Dict], year: Optional[int] = None) -> bool:
    """True if any filing-history item has a date in `year` (default: this year)."""
    target = year if year is not None else date.today().year
    prefix = f"{target}-"
    for item in items or []:
        filed = item.get("date") or ""
        if isinstance(filed, str) and filed.startswith(prefix):
            return True
    return False


def _has_current_year_filing(company_number: str) -> bool:
    data = _get(
        f"/company/{company_number}/filing-history",
        params={"items_per_page": 25},
    )
    if not data:
        return False
    return has_filing_in_year(data.get("items") or [])


def _find_best_company(business_name: str, category: str = "") -> Optional[Dict]:
    """Only ever returns a live, actively-trading company. The search index's
    company_status field can lag, so it's used as a cheap first filter here and
    re-verified against the authoritative company profile below before returning."""
    data = _get("/search/companies", params={"q": business_name, "items_per_page": 8})
    if not data:
        return None

    candidates = []
    for item in data.get("items", []):
        title = item.get("title", "")
        if not status_acceptable(item.get("company_status")):
            continue
        if not _plausible_match(business_name, title):
            continue
        score = _name_similarity(business_name, title)
        candidates.append((score, item))
    candidates.sort(key=lambda c: c[0], reverse=True)

    for _, item in candidates[:5]:  # cap profile lookups per business
        profile = _get_profile(item["company_number"])
        if not profile or not status_acceptable(profile.get("company_status")):
            continue

        # "active" alone can lag — a company can go quiet (stop trading, miss
        # filings) long before Companies House formally dissolves it. Overdue
        # accounts is the earliest public signal that a company on paper may
        # not actually be a live business worth calling.
        accounts = profile.get("accounts") or {}
        if accounts.get("overdue"):
            continue

        if not _sic_matches_category(profile.get("sic_codes", []), category):
            continue

        # No filing in the current calendar year → treat like dissolved.
        if not _has_current_year_filing(item["company_number"]):
            continue

        item = dict(item)
        item["_last_accounts_made_up_to"] = (accounts.get("last_accounts") or {}).get("made_up_to")
        item["_registered_office"] = _format_registered_office(
            profile.get("registered_office_address")
        )
        item["_sic_codes"] = list(profile.get("sic_codes") or [])
        return item
    return None


_OFFICER_ROLE_ALLOW = {"director", "member", "llp-designated-member", "llp-member"}
_CORPORATE_NAME_RE = re.compile(
    r"\b(ltd|limited|plc|llp|llc|inc|company|nominees?|formations?|secretarial)\b",
    re.I,
)
_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", re.I)


def _format_registered_office(addr: Optional[Dict]) -> str:
    if not addr:
        return ""
    parts = [
        addr.get("address_line_1"),
        addr.get("address_line_2"),
        addr.get("locality"),
        addr.get("region"),
        addr.get("postal_code"),
        addr.get("country"),
    ]
    return ", ".join(p for p in parts if p)


def addresses_mismatch(trading: str, registered: str) -> bool:
    """True when both addresses have UK postcodes that disagree (likely
    shopfront vs accountant registered office). Silent if either lacks a postcode."""
    if not (trading or "").strip() or not (registered or "").strip():
        return False
    t_pc = _POSTCODE_RE.search(trading)
    r_pc = _POSTCODE_RE.search(registered)
    if not t_pc or not r_pc:
        return False
    norm = lambda m: re.sub(r"\s+", "", m.group(1)).upper()
    return norm(t_pc) != norm(r_pc)


def _format_officer_name(raw: str) -> str:
    # Companies House format is "SURNAME, Forename Middle"
    if "," in raw:
        surname, _, rest = raw.partition(",")
        surname = surname.strip().title()
        rest = rest.strip()
        if not rest:
            return surname
        forenames = " ".join(p.capitalize() for p in rest.split())
        return f"{forenames} {surname}"
    return raw.strip().title()


def _is_corporate_officer(o: Dict) -> bool:
    role = (o.get("officer_role") or "").lower()
    if "corporate" in role:
        return True
    ident = o.get("identification") or {}
    if ident.get("registration_number") or ident.get("place_registered"):
        return True
    return bool(_CORPORATE_NAME_RE.search(o.get("name") or ""))


def _get_individual_psc(company_number: str) -> Optional[str]:
    data = _get(
        f"/company/{company_number}/persons-with-significant-control",
        params={"items_per_page": 10},
    )
    if not data:
        return None
    for item in data.get("items", []):
        if item.get("ceased_on"):
            continue
        kind = (item.get("kind") or "").lower()
        if "individual" not in kind:
            continue
        name = item.get("name")
        if name and not _CORPORATE_NAME_RE.search(name):
            return _format_officer_name(name)
    return None


def _get_active_officer(company_number: str) -> Optional[str]:
    data = _get(f"/company/{company_number}/officers", params={"items_per_page": 10})
    if not data:
        return None
    candidates = [
        o for o in data.get("items", [])
        if o.get("officer_role") in _OFFICER_ROLE_ALLOW
        and not o.get("resigned_on")
        and o.get("name")
        and not _is_corporate_officer(o)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda o: o.get("appointed_on") or "", reverse=True)
    return _format_officer_name(candidates[0]["name"])


def find_owner(business_name: str, category: str = "", cancel_event=None) -> Optional[Dict]:
    """Last-resort owner lookup via Companies House.

    Returns a dict with name, evidence, source, registered_office, company_number
    — or None if no key / no confident company match. Prefer individual PSC over
    directors; skip corporate/nominee-style officers. registered_office and
    company_number are filled when a company matches (even if no person name).
    """
    if not COMPANIES_HOUSE_API_KEY or not business_name:
        return None
    if cancel_event is not None and cancel_event.is_set():
        return None

    company = _find_best_company(business_name, category)
    if not company:
        return None
    if cancel_event is not None and cancel_event.is_set():
        return None

    company_number = company["company_number"]
    office = company.get("_registered_office") or ""
    sic_codes = list(company.get("_sic_codes") or [])
    title = company.get("title") or company_number
    last_accounts = company.get("_last_accounts_made_up_to")
    accounts_note = f", accounts filed to {last_accounts}" if last_accounts else ""

    psc_name = _get_individual_psc(company_number)
    if psc_name:
        return {
            "name": psc_name,
            "evidence": (
                f"PSC found via Companies House ({title}, active{accounts_note})"
            ),
            "source": "companies_house_psc",
            "registered_office": office,
            "company_number": company_number,
            "sic_codes": sic_codes,
        }

    officer_name = _get_active_officer(company_number)
    if officer_name:
        return {
            "name": officer_name,
            "evidence": (
                f"director found via Companies House ({title}, active{accounts_note})"
            ),
            "source": "companies_house_director",
            "registered_office": office,
            "company_number": company_number,
            "sic_codes": sic_codes,
        }

    return {
        "name": "",
        "evidence": (
            f"Companies House match ({title}, active{accounts_note}) "
            "but no individual PSC/director"
        ),
        "source": "none",
        "registered_office": office,
        "company_number": company_number,
        "sic_codes": sic_codes,
    }


if __name__ == "__main__":
    assert status_acceptable("active")
    assert not status_acceptable("dissolved")
    assert not status_acceptable("liquidation")
    assert not status_acceptable("active-proposal-to-strike-off")
    assert not status_acceptable("dormant")
    assert has_filing_in_year([{"date": "2026-03-01"}], 2026)
    assert not has_filing_in_year([{"date": "2023-03-01"}], 2026)
    assert not has_filing_in_year([], 2026)
    print("ok")
