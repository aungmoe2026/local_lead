"""
Enrich each listing by crawling its website (if it has one) for:
  - an owner/proprietor name (preferred)
  - failing that: partner / director / named contact
  - phone + email as fallback when OSM didn't have them

Owner/partner name is treated as mandatory for lead quality. Extraction is
tiered: owner phrases first, then partner/director roles, then softer
signals (JSON-LD Person, email local-part like jane.smith@...).

Never invent a random name — only fill when a pattern matches cleanly.
"""

import json
import re
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

import companies_house
import js_render
from config import (
    CONTACT_PATHS,
    JS_RENDER_MIN_TEXT_LENGTH,
    MAX_CONSECUTIVE_SITE_FAILURES,
    MAX_CONSECUTIVE_SITE_FAILURES_WITHOUT_NAME,
    SITE_CRAWL_DELAY_SECONDS,
    SITE_REQUEST_TIMEOUT_SECONDS,
    SITE_USER_AGENT,
)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(?:\+44\s?\d{2,4}|\(?0\d{2,4}\)?)[\s\-]?\d{3,4}[\s\-]?\d{3,4}")

_PERSON = r"[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+){0,2}"
NAME_PATTERN = rf"(?:(?i:the|a|mr\.?|mrs\.?|ms\.?|miss|dr\.?)\s+)?{_PERSON}(?:(?i:\s+and\s+){_PERSON})?"

# Tier 1 — explicit owner / proprietor
OWNER_PATTERNS = [
    rf"(?i:owned and (?:run|operated) by|owned by|owner[s]?\s*[:\-]|proprietor[s]?\s*[:\-]|"
    rf"meet (?:the |our )?owner|your host[s]?\s*[:\-]|"
    rf"family[- ]run by|"
    rf"licensee[s]?\s*[:\-]|landlord[s]?\s*[:\-])\s*({NAME_PATTERN})",
    rf"(?i:founded by|founder[s]?\s*[:\-]|co[- ]?founder[s]?\s*[:\-])\s*({NAME_PATTERN})",
]

# Tier 2 — partner / director (acceptable substitute). Keep tight — loose
# "welcome from X" patterns invent junk names from marketing copy.
PARTNER_PATTERNS = [
    rf"(?i:partner[s]?\s*[:\-]|managing partner|business partner|"
    rf"co[- ]?owner[s]?|joint owner)\s*[:\-]?\s*({NAME_PATTERN})",
    rf"(?i:director[s]?\s*[:\-]|managing director|\bmd\s*[:\-]|"
    rf"general manager|chef patron)\s*[:\-]?\s*({NAME_PATTERN})",
    rf"(?i:run by|managed by|operated by|looked after by)\s+({NAME_PATTERN})",
    rf"({_PERSON})\s*,?\s*(?i:owner|co[- ]?owner|partner|director|proprietor|licensee)\b",
]

GENERIC_WORDS = {
    "Contact Us", "About Us", "Our Story", "Read More", "Find Out",
    "Book Now", "Opening Hours", "Terms Conditions", "Learn More",
    "Get In", "Click Here", "Find Us", "Follow Us", "Sign Up",
    "Privacy Policy", "Cookie Policy", "Home Page", "Main Menu",
    "Limited Company", "All Rights", "Reserved", "Web Design",
    "Your Code", "Dukungan Nonstop", "Pizza Express",
}

NAME_TOKEN_BLOCKLIST = {
    "Limited", "Ltd", "Plc", "Company", "Restaurant", "Cafe", "Café",
    "Hotel", "Pub", "Bar", "Kitchen", "House", "Group", "Holdings",
    "England", "Scotland", "Wales", "London", "Manchester", "Leeds",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
    "Your", "Code", "Support", "Team", "Welcome", "Click", "Here",
    "Privacy", "Cookie", "Policy", "Express", "Italia", "Italian",
    "Pizza", "Dukungan", "Nonstop", "Booking", "Reserve", "Menu",
    "Home", "Page", "Main", "About", "Contact", "Follow", "Share",
    "Instagram", "Facebook", "Twitter", "Linkedin", "Youtube",
    "Free", "Delivery", "Takeaway", "Online", "Website", "Design",
    "Studio", "Agency", "Solutions", "Services", "International",
    "Ask", "Bella", "Italia",
}

NAME_BLOCKLIST = NAME_TOKEN_BLOCKLIST

GENERIC_EMAIL_LOCALS = {
    "info", "hello", "contact", "enquiries", "enquiry", "admin", "office",
    "support", "sales", "bookings", "booking", "reservations", "reservation",
    "mail", "email", "webmaster", "noreply", "no-reply", "donotreply",
    "accounts", "hr", "jobs", "press", "media", "help", "team", "host",
    "ouroffice", "general", "reception", "manager", "owners",
}

JUNK_EMAIL_FRAGMENTS = (
    "example.com", "sentry.io", "wixpress.com", "schema.org",
    "wordpress.com", "godaddy.com", "email.com", "domain.com",
    "yourname@", "you@", "name@", ".png", ".jpg", ".gif", ".svg",
    "w3.org", "googleapis.com", "cloudflare",
)

BRAND_OPERATOR_RE = re.compile(
    r"\b(ltd|limited|plc|inc|llc|group|holdings|company|restaurants?|"
    r"cafes?|hotels?|pubs?|bars?|express|italia|italian|pizza|ask)\b",
    re.I,
)


def normalize_url(website: str) -> Optional[str]:
    if not website:
        return None
    website = website.strip()
    if not website.startswith(("http://", "https://")):
        website = "https://" + website
    return website


def _is_junk_email(email: str) -> bool:
    lower = email.lower()
    return any(j in lower for j in JUNK_EMAIL_FRAGMENTS)


def _looks_like_person_name(candidate: str, business_name: str = "") -> bool:
    if not candidate:
        return False
    candidate = candidate.strip(" ,.-–—|")
    if candidate in GENERIC_WORDS:
        return False
    parts = candidate.split()
    if not (1 <= len(parts) <= 6):
        return False
    if any(p in NAME_TOKEN_BLOCKLIST for p in parts):
        return False
    if business_name and candidate.lower() == business_name.strip().lower():
        return False
    alpha_parts = [
        p for p in parts
        if p.lower() != "and" and re.match(r"^[A-Za-z][A-Za-z'\-]+$", p)
    ]
    if not (2 <= len(alpha_parts) <= 4):
        return False
    # Each real name token should be title-ish (reject ALLCAPS brands)
    for p in alpha_parts:
        if p.isupper() and len(p) > 3:
            return False
    return True


def _clean_name(raw: str, business_name: str = "") -> Optional[str]:
    candidate = raw.strip()
    candidate = re.sub(
        r"^(?:the|a|mr\.?|mrs\.?|ms\.?|miss|dr\.?)\s+",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(r"\s+", " ", candidate).strip(" ,.-")
    if _looks_like_person_name(candidate, business_name):
        return candidate
    return None


_SPA_FINGERPRINT_RE = re.compile(
    r'id=["\'](?:root|app|__next|__nuxt)["\']|<app-root|<router-outlet', re.IGNORECASE
)


def _visible_text_length(html: str) -> int:
    without_scripts = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", without_scripts)
    return len(re.sub(r"\s+", " ", text).strip())


def _looks_js_rendered(html: Optional[str]) -> bool:
    """True when a plain requests.get() likely returned an empty JS shell (or
    failed outright) and it's worth retrying with a real headless browser."""
    if not html:
        return True
    visible_len = _visible_text_length(html)
    if visible_len >= JS_RENDER_MIN_TEXT_LENGTH:
        return False
    has_spa_marker = bool(_SPA_FINGERPRINT_RE.search(html))
    has_big_script_near_empty_body = len(html) > 2000 and visible_len < 60
    return has_spa_marker or has_big_script_near_empty_body


def _fetch(session: requests.Session, url: str) -> Optional[str]:
    candidates = [url]
    if url.startswith("https://"):
        candidates.append("http://" + url[len("https://"):])

    for candidate in candidates:
        try:
            resp = session.get(
                candidate,
                timeout=SITE_REQUEST_TIMEOUT_SECONDS,
                allow_redirects=True,
            )
            if resp.status_code == 200 and resp.text:
                return resp.text[:400_000]
        except requests.RequestException:
            continue
    return None


def _match_patterns(text: str, patterns, business_name: str) -> Optional[str]:
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            cleaned = _clean_name(match.group(1), business_name)
            if cleaned:
                return cleaned
    return None


def _extract_from_json_ld(html: str, business_name: str) -> Optional[Tuple[str, str]]:
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        blobs = data if isinstance(data, list) else [data]
        for blob in blobs:
            if not isinstance(blob, dict):
                continue
            for key in ("founder", "founders", "employee", "employees", "member", "author"):
                val = blob.get(key)
                if not val:
                    continue
                people = val if isinstance(val, list) else [val]
                for person in people:
                    name = person.get("name") if isinstance(person, dict) else person
                    if isinstance(name, str):
                        cleaned = _clean_name(name, business_name)
                        if cleaned:
                            return cleaned, "json-ld"
            if blob.get("@type") in ("Person", ["Person"]) and blob.get("name"):
                cleaned = _clean_name(str(blob["name"]), business_name)
                if cleaned:
                    return cleaned, "json-ld"
    return None


def _name_from_email(email: str, business_name: str = "") -> Optional[str]:
    if not email or _is_junk_email(email):
        return None
    local = email.split("@", 1)[0].lower()
    local = unquote(local).split("+", 1)[0]
    if local in GENERIC_EMAIL_LOCALS:
        return None
    if re.search(r"\d{3,}", local):
        return None
    parts = re.split(r"[._\-]+", local)
    parts = [p for p in parts if p and p.isalpha() and len(p) >= 2]
    if len(parts) < 2 or len(parts) > 3:
        return None
    if any(p in GENERIC_EMAIL_LOCALS for p in parts):
        return None
    candidate = " ".join(p.capitalize() for p in parts)
    return _clean_name(candidate, business_name)


def _osm_operator_as_person(operator: str, business_name: str) -> Optional[str]:
    if not operator:
        return None
    if BRAND_OPERATOR_RE.search(operator):
        return None
    return _clean_name(operator, business_name)


def _extract_person(text: str, html: str, business_name: str) -> Optional[Tuple[str, str]]:
    owner = _match_patterns(text, OWNER_PATTERNS, business_name)
    if owner:
        return owner, "owner"

    partner = _match_patterns(text, PARTNER_PATTERNS, business_name)
    if partner:
        return partner, "partner"

    if html:
        jd = _extract_from_json_ld(html, business_name)
        if jd:
            return jd[0], "partner"

    return None


def enrich_listing(row: Dict, cancel_event=None) -> Dict:
    """Find owner (preferred) or partner/contact name + phone/email.

    Source of truth: website owner wins when found; Companies House always
    runs afterward for live-company office/owner fallback (never overwrites
    a website/OSM owner name).
    """
    result = {
        "owner_name": "",
        "owner_evidence": "",
        "owner_source": "none",
        "ch_registered_office": "",
        "company_number": "",
        "sic_code": "",
    }
    business_name = row.get("business_name", "") or ""
    website = normalize_url(row.get("website", ""))

    found_owner = None
    found_evidence = ""
    found_source = "none"
    ch_office = ""
    company_number = ""
    sic_code = ""
    found_phone = row.get("phone", "") or ""
    found_email = row.get("email", "") or ""

    def apply_ch(ch: Optional[Dict], *, fill_owner: bool) -> None:
        """Attach live CH office + company number; optionally fill owner if empty."""
        nonlocal found_owner, found_evidence, found_source, ch_office, company_number, sic_code
        if not ch:
            return
        ch_office = ch.get("registered_office") or ch_office
        company_number = ch.get("company_number") or company_number
        codes = ch.get("sic_codes") or []
        if codes and not sic_code:
            sic_code = str(codes[0])
        if fill_owner and ch.get("name") and not found_owner:
            found_owner = ch["name"]
            found_evidence = ch.get("evidence") or found_evidence
            found_source = ch.get("source") or "companies_house_director"

    def run_ch() -> None:
        apply_ch(
            companies_house.find_owner(
                business_name, row.get("category", ""), cancel_event=cancel_event
            ),
            fill_owner=not found_owner,
        )

    osm_person = _osm_operator_as_person(row.get("operator", "") or "", business_name)
    if osm_person:
        found_owner = osm_person
        found_evidence = "from OpenStreetMap owner/operator tag"
        found_source = "osm"

    if not website:
        if not found_owner and found_email:
            inferred = _name_from_email(found_email, business_name)
            if inferred:
                found_owner = inferred
                found_evidence = f"inferred from email {found_email}"
                found_source = "website"
        run_ch()
        result["owner_name"] = found_owner or ""
        result["owner_evidence"] = found_evidence or "no website listed"
        result["owner_source"] = found_source if found_owner else "none"
        result["ch_registered_office"] = ch_office
        result["company_number"] = company_number
        result["sic_code"] = sic_code
        result["phone"] = found_phone
        result["email"] = found_email
        return result

    parsed = urlparse(website)
    root = f"{parsed.scheme}://{parsed.netloc}"
    consecutive_failures = 0

    session = requests.Session()
    session.headers.update({
        "User-Agent": SITE_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    })

    try:
        for path in CONTACT_PATHS:
            if found_owner and found_phone and found_email:
                break

            fail_limit = (
                MAX_CONSECUTIVE_SITE_FAILURES
                if found_owner
                else MAX_CONSECUTIVE_SITE_FAILURES_WITHOUT_NAME
            )
            if consecutive_failures >= fail_limit:
                found_evidence = found_evidence or (
                    "website too slow/unreachable, skipped remaining pages"
                )
                break
            if cancel_event is not None and cancel_event.is_set():
                found_evidence = found_evidence or (
                    "stopped by user before finishing this site"
                )
                break

            url = urljoin(root + "/", path)
            html = _fetch(session, url)
            if SITE_CRAWL_DELAY_SECONDS > 0:
                time.sleep(SITE_CRAWL_DELAY_SECONDS)

            if _looks_js_rendered(html):
                rendered = js_render.render(url)
                if rendered:
                    html = rendered

            if not html:
                consecutive_failures += 1
                continue
            consecutive_failures = 0

            if not found_owner:
                jd = _extract_from_json_ld(html, business_name)
                if jd:
                    found_owner = jd[0]
                    found_evidence = f"partner found on {url} (structured data)"
                    found_source = "website"

            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "noscript", "svg"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
            text = re.sub(r"\s+", " ", text)

            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if href.lower().startswith("mailto:") and not found_email:
                    addr = href.split(":", 1)[1].split("?", 1)[0].strip()
                    if addr and not _is_junk_email(addr):
                        found_email = addr
                elif href.lower().startswith("tel:") and not found_phone:
                    found_phone = href.split(":", 1)[1].strip()

            if not found_owner:
                person = _extract_person(text, "", business_name)
                if person:
                    found_owner, role = person
                    found_evidence = f"{role} found on {url}"
                    found_source = "website"

            if not found_phone:
                phone_match = PHONE_RE.search(text)
                if phone_match:
                    found_phone = phone_match.group(0).strip()

            if not found_email:
                for email_match in EMAIL_RE.finditer(text):
                    candidate = email_match.group(0).strip()
                    if not _is_junk_email(candidate):
                        found_email = candidate
                        break
    finally:
        session.close()

    if not found_owner and found_email:
        inferred = _name_from_email(found_email, business_name)
        if inferred:
            found_owner = inferred
            found_evidence = f"partner/contact inferred from email {found_email}"
            found_source = "website"

    # Always attempt CH: office for mismatch; owner only if still empty.
    run_ch()

    result["owner_name"] = found_owner or ""
    if found_owner:
        result["owner_evidence"] = found_evidence or "name found"
        result["owner_source"] = found_source
    else:
        result["owner_evidence"] = (
            found_evidence or "no owner or partner name found on site"
        )
        result["owner_source"] = "none"
    result["ch_registered_office"] = ch_office
    result["company_number"] = company_number
    result["sic_code"] = sic_code
    result["phone"] = found_phone
    result["email"] = found_email
    return result
