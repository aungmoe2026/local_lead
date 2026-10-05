"""
Configuration for the local lead-gen tool.

Core pipeline runs on free, keyless public services:
  - Nominatim (OpenStreetMap geocoding) - turns "Ruislip, UK" into lat/lon
  - Overpass API (OpenStreetMap data) - returns business listings

Just be a good citizen of these free shared services: identify yourself with
a real User-Agent and don't hammer them with requests (delays below are
deliberately conservative).

Optional: Companies House API (owner-name fallback) does need a key. Put it
in backend/.env (see .env.example) as COMPANIES_HOUSE_API_KEY=... — never
hardcode it here or anywhere in source.
"""

import os
import re


def _load_env_file():
    """Tiny stdlib-only .env loader — avoids adding python-dotenv as a
    dependency."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_env_file()

# Nominatim/Overpass usage policy requires a descriptive User-Agent
# (not a generic browser string). Edit the contact bit if you like.
USER_AGENT = "local-leadgen-tool/1.0 (personal use; contact: none)"

# Business websites often block non-browser UAs. Use a normal browser string
# ONLY for site enrichment crawls — Nominatim/Overpass still get USER_AGENT above.
SITE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Primary + mirrors. Jobs try the next mirror on timeout / 5xx so one
# overloaded Overpass instance doesn't kill a whole lead run.
OVERPASS_URLS = [
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
]
# Keep a single URL alias for anything that still imports OVERPASS_URL.
OVERPASS_URL = OVERPASS_URLS[0]

REQUEST_TIMEOUT_SECONDS = 20
NOMINATIM_DELAY_SECONDS = 1.1     # Nominatim policy: max 1 request/second
SITE_CRAWL_DELAY_SECONDS = 0.12   # short politeness gap; concurrency is the speed lever

# --- Auto radius expansion ---
# Overpass (unlike Google Places) has no artificial results-per-query cap, so
# to reliably hit a target lead count in one click, we just widen the search
# radius automatically when the first pass comes back short, instead of
# making the user guess the right radius up front.
DEFAULT_TARGET_LEADS = 100
DEFAULT_RADIUS_KM = 12          # start wider so fewer expand rounds (= faster)
MAX_RADIUS_KM = 50              # don't expand past this even if target isn't met
MAX_RADIUS_EXPANSIONS = 5       # safety cap on how many times we'll widen the search
RADIUS_EXPANSION_FACTOR = 1.8

# --- Enrichment concurrency ---
# Website crawling (owner/phone/email lookup) is I/O-bound and each business's
# site is an independent request, so we fetch several in parallel to keep a
# 100+ lead run from taking forever. Measured on a real 200-business run:
# at 32 workers / 7s timeout, ~60% of genuinely-live small-biz sites timed
# out purely from connection contention (retested individually, most came
# back fine well under 12s). Trimmed workers and loosened the timeout to
# recover those instead of writing the sites off as dead.
ENRICHMENT_WORKERS = 20

# Small business sites are often slow or half-dead. A long per-request
# timeout here is the single biggest cause of a run feeling stuck - so this
# is deliberately much shorter than REQUEST_TIMEOUT_SECONDS above (which is
# used for Nominatim/Overpass, where you actually want to wait for a full
# answer). If a page hasn't responded in this long, better to move on.
SITE_REQUEST_TIMEOUT_SECONDS = 12

# If a business's website fails this many page fetches in a row (down/dead/
# too slow), stop trying more pages for it rather than working through the
# whole CONTACT_PATHS list at the same bad odds.
MAX_CONSECUTIVE_SITE_FAILURES = 3

# Keep crawling a bit longer when we still have no owner/partner name —
# that field is mandatory for lead quality.
MAX_CONSECUTIVE_SITE_FAILURES_WITHOUT_NAME = 4

# How many finished leads to keep in job status for the live preview table.
PREVIEW_ROW_LIMIT = 25

# After quality sort, keep at most target + this many (dense Maps batches overshoot).
TARGET_LEAD_OVERAGE = 30

# OSM tag combinations for each category. A listing matches a category if it
# has ANY of the tag=value pairs listed (value "*" means "any value" for that key).
# Maps discovery primarily uses CATEGORY_SEARCH_LABELS / synonyms; OSM tags are
# for any leftover Overpass path.
CATEGORIES = {
    "garage":     [("shop", "car"), ("shop", "car_repair"), ("craft", "motor")],
    "fabric":     [("shop", "fabric"), ("shop", "curtain")],
    "fish_chips": [("amenity", "fast_food"), ("cuisine", "fish_and_chips")],
    "florist":    [("shop", "florist")],
    "tattoo":     [("shop", "tattoo")],
    "jewellery":  [("shop", "jewelry"), ("shop", "jewellery")],
    "butcher":    [("shop", "butcher")],
    "tanning":    [("shop", "beauty"), ("leisure", "tanning_salon")],
    "wool_craft": [("shop", "wool"), ("shop", "craft"), ("shop", "haberdashery")],
}

# Common website paths to check for owner/partner/contact names.
# Homepage + about/team pages first — highest yield for a person name.
CONTACT_PATHS = [
    "", "about", "about-us", "our-story", "team", "contact", "contact-us",
]

# --- JS-rendering fallback (crawl4ai / Playwright) ---
# Many small-biz sites (Wix/Squarespace/React) return a near-empty HTML shell
# to a plain requests.get() — real content only shows up after client JS
# runs. contact_enricher.py tries the cheap requests path first for every
# site and only falls back to a real headless-browser render (js_render.py)
# when that looks like it hit a JS shell. Off entirely disables the fallback
# (e.g. if Playwright's browser binary isn't installed on this machine).
JS_RENDER_ENABLED = True

# Headless Chromium is much heavier than a requests.get() — keep real render
# concurrency well below ENRICHMENT_WORKERS regardless of how many enrichment
# threads are running (most sites never need this path at all).
JS_RENDER_MAX_CONCURRENCY = 4
JS_RENDER_TIMEOUT_SECONDS = 15

# If a requests.get() returned fewer than this many characters of visible
# text, treat it as a likely JS shell and worth a render retry.
JS_RENDER_MIN_TEXT_LENGTH = 200

# --- Companies House (owner-name fallback for registered companies) ---
# Optional — leave unset and this tier is skipped entirely (sole traders,
# which most small local businesses are, won't have a record here anyway).
# Get a free key: https://developer.company-information.service.gov.uk
COMPANIES_HOUSE_API_KEY = os.environ.get("COMPANIES_HOUSE_API_KEY", "").strip()

# Companies House caps usage at 600 requests / 5 min and bans apps that
# "regularly exceed or attempt to bypass" it. Stay well under that cap
# regardless of how many enrichment workers are hammering it at once.
COMPANIES_HOUSE_SAFE_REQUEST_LIMIT = 500   # cap per window, not the hard 600
COMPANIES_HOUSE_WINDOW_SECONDS = 300       # matches their 5-minute window
COMPANIES_HOUSE_REQUEST_TIMEOUT_SECONDS = 10

# --- Apify (optional paid data source: live Google Maps listings) ---
# Optional — leave unset and the "Google Maps" source stays unavailable in the
# UI; OSM/Overpass remains the default, free source. Google Maps' own open/
# closed status is generally far fresher than OSM (crowdsourced, can lag
# months behind a real closure), so this is worth the cost when lead quality
# matters more than it being free. Get a token: https://console.apify.com
APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN", "").strip()
APIFY_ACTOR_ID = "compass~crawler-google-places"

# Runs are started async and polled, not via Apify's "run-sync" endpoint —
# that endpoint always hard-caps at 300s regardless of plan/payment, so a
# thorough crawl (many categories, big lead target) would get cut off. Async
# has no such ceiling; these three just bound our own polling behaviour.
APIFY_ACTOR_RUN_TIMEOUT_SECONDS = 600   # how long Apify itself lets the actor run before killing it
APIFY_POLL_INTERVAL_SECONDS = 4         # how often we check run status while waiting
APIFY_MAX_WAIT_SECONDS = 1200           # our own safety bound so a stuck run doesn't hang a job forever
APIFY_HTTP_TIMEOUT_SECONDS = 30         # per HTTP call (start/poll/abort/fetch) — not the whole crawl

# Search phrase per category key — place goes in locationQuery /
# customGeolocation, not inside these phrases.
CATEGORY_SEARCH_LABELS = {
    "garage": "garage",
    "fabric": "fabric shop",
    "fish_chips": "fish and chips",
    "florist": "florist",
    "tattoo": "tattoo shop",
    "jewellery": "jeweller",
    "butcher": "butcher",
    "tanning": "tanning salon",
    "wool_craft": "wool shop",
}

# Extra Google Maps phrases per category (fan-out for city-scale coverage).
CATEGORY_PHRASE_SYNONYMS = {
    "garage": ["garage", "MOT", "car repair", "tyre"],
    "fabric": ["fabric shop", "dress fabric", "curtain fabric"],
    "fish_chips": ["fish and chips", "chip shop"],
    "florist": ["florist", "flower shop"],
    "tattoo": ["tattoo shop", "tattoo studio"],
    "jewellery": ["jeweller", "jewellery shop", "jewelry store"],
    "butcher": ["butcher", "butchers shop"],
    "tanning": ["tanning salon", "tanning shop", "sunbed"],
    "wool_craft": ["wool shop", "craft shop", "haberdashery"],
}

# Free-text "Also match by name" expansion. Keys are normalised stems
# (lowercase, trailing plural-s stripped). Values are distinct Maps phrases.
NAME_TERM_SYNONYMS = {
    "garage": ["garage", "MOT", "car repair", "auto repair", "tyre", "vehicle service"],
    "mot": ["MOT", "garage", "car repair", "vehicle service"],
    "tyre": ["tyre", "tyre fitting", "garage"],
    "tire": ["tyre", "tyre fitting", "garage"],
    "fabric": ["fabric shop", "dress fabric", "curtain fabric"],
    "florist": ["florist", "flower shop"],
    "flower": ["florist", "flower shop"],
    "tattoo": ["tattoo shop", "tattoo studio"],
    "jeweller": ["jeweller", "jewellery shop"],
    "jewelry": ["jeweller", "jewellery shop"],
    "jewellery": ["jeweller", "jewellery shop"],
    "butcher": ["butcher", "butchers shop"],
    "tanning": ["tanning salon", "sunbed"],
    "wool": ["wool shop", "craft shop", "haberdashery"],
    "craft": ["craft shop", "wool shop", "haberdashery"],
    "chip": ["fish and chips", "chip shop"],
    "mechanic": ["mechanic", "car repair", "garage", "auto repair"],
    "plumber": ["plumber", "plumbing"],
    "electrician": ["electrician", "electrical contractor"],
    "builder": ["builder", "building contractor"],
    "dentist": ["dentist", "dental practice"],
    "solicitor": ["solicitor", "law firm"],
    "accountant": ["accountant", "accountancy"],
}

# Satellite ring around the geocoded centre when radius expansion still
# undershoots the lead target (wider geographic discovery).
SATELLITE_RING_POINTS = 6
SATELLITE_RING_RADIUS_FACTOR = 0.6   # ring distance = current_radius * this
SATELLITE_POINT_RADIUS_FACTOR = 0.5  # each satellite crawl radius

# --- Chain / franchise filter ---
# The tool is aimed at small independent businesses, not national/global
# chains. An OSM listing for a franchise usually carries a "brand" tag
# (checked directly in overpass_search.py — more precise than name matching);
# this list is the fallback for sources without that tag (Google Maps via
# Apify) and for franchises OSM mappers didn't tag. Matched as a substring of
# the normalized (lowercased, punctuation-stripped) business name.
CHAIN_BLOCKLIST = {
    # fast food / coffee / bakery
    "mcdonald", "burger king", "kfc", "kentucky fried chicken", "taco bell",
    "subway", "starbucks", "costa coffee", "caffe nero", "cafe nero",
    "greggs", "pret a manger", "dominos", "domino's", "pizza hut",
    "papa johns", "papa john's", "five guys", "nandos", "nando's",
    "chipotle", "wendy's", "wendys", "dunkin", "leon restaurants",
    "cake box", "cakebox",
    # pubs / bars / hotels (national chains)
    "wetherspoon", "jd wetherspoon", "premier inn", "travelodge",
    "holiday inn", "ibis hotel", "hilton", "marriott", "novotel",
    "toby carvery", "harvester", "beefeater", "brewers fayre",
    "all bar one", "slug and lettuce", "the botanist", "young's pubs",
    # supermarkets / retail
    "tesco", "asda", "sainsbury", "morrisons", "aldi", "lidl", "iceland",
    "co-op food", "waitrose", "marks and spencer", "m&s", "boots",
    "superdrug", "wilko", "b&q", "screwfix", "argos", "wh smith",
    "poundland", "home bargains", "the range", "specsavers", "vision express",
    # auto / high-street franchises (AM niches)
    "kwik fit", "kwikfit", "halfords", "halfords autocentre", "ats euromaster",
    "national tyre", "formula one autocentres",
    "timpson", "timpsons", "snappy snaps",
    "clarks", "clinton cards", "card factory",
    "h samuel", "ernest jones", "fraser hart", "beaverbrooks",
    "thorntons", "hotel chocolat", "interflora",
    "the body shop",
    # gyms / salons / tanning chains
    "puregym", "pure gym", "the gym group", "anytime fitness", "david lloyd",
    "toni and guy", "toni & guy", "regis salon",
    "the tanning shop", "planet sun", "banana moon",
}

# Drop HQ / admin sites (not a trading high-street shop).
HEAD_OFFICE_NAME_RE = re.compile(
    r"\b(head office|hq|h\.?q\.?|registered office|national office|group office|"
    r"headquarters|admin office|corporate office)\b",
    re.I,
)

# Drop retail-park / mall locations — AM targets high-street independents.
RETAIL_PARK_PHRASES = (
    "retail park",
    "shopping centre",
    "shopping center",
    "outlet village",
    "retail outlet",
    "shopping mall",
    "outlet centre",
    "outlet center",
    "designer outlet",
)
