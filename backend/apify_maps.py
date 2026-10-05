"""Google Maps data via Apify's Google Maps Scraper actor.

Rows are shaped like overpass_search rows so enrichment/CSV stay source-agnostic.

Runs are started asynchronously and polled (Apify's sync endpoint hard-caps
at 300s). Wider discovery uses bare search phrases + locationQuery /
customGeolocation, synonym fan-out, and optional satellite offset crawls
orchestrated by jobs.py.
"""

import math
import re
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import requests

import chain_filter
import opening_hours
from config import (
    APIFY_ACTOR_ID,
    APIFY_ACTOR_RUN_TIMEOUT_SECONDS,
    APIFY_API_TOKEN,
    APIFY_HTTP_TIMEOUT_SECONDS,
    APIFY_MAX_WAIT_SECONDS,
    APIFY_POLL_INTERVAL_SECONDS,
    CATEGORY_PHRASE_SYNONYMS,
    CATEGORY_SEARCH_LABELS,
    NAME_TERM_SYNONYMS,
    SATELLITE_POINT_RADIUS_FACTOR,
    SATELLITE_RING_POINTS,
    SATELLITE_RING_RADIUS_FACTOR,
)

RUNS_URL = f"https://api.apify.com/v2/actors/{APIFY_ACTOR_ID}/runs"
RUN_STATUS_URL = "https://api.apify.com/v2/actor-runs/{run_id}"
RUN_ABORT_URL = "https://api.apify.com/v2/actor-runs/{run_id}/abort"
DATASET_ITEMS_URL = "https://api.apify.com/v2/datasets/{dataset_id}/items"

_TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}
_STEM_RE = re.compile(r"[^a-z0-9]+")


class ApifyError(Exception):
    """Raised when the Apify actor call fails, times out, or isn't configured."""


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {APIFY_API_TOKEN}"}


def _stem(text: str) -> str:
    s = _STEM_RE.sub("", (text or "").lower())
    if len(s) > 3 and s.endswith("s") and not s.endswith("ss"):
        s = s[:-1]
    return s


def _expand_name_term(term: str) -> List[str]:
    raw = (term or "").strip()
    if not raw:
        return []
    stem = _stem(raw)
    for key, phrases in NAME_TERM_SYNONYMS.items():
        if stem == key or stem.startswith(key) or key.startswith(stem):
            return list(phrases)
    return [raw]


def build_search_phrases(categories: List[str], name_terms: List[str]) -> List[str]:
    """Bare Google Maps phrases (no 'in {place}'); place is locationQuery/geo."""
    phrases: List[str] = []
    for c in categories or []:
        syns = CATEGORY_PHRASE_SYNONYMS.get(c)
        if syns:
            phrases.extend(syns)
        else:
            phrases.append(CATEGORY_SEARCH_LABELS.get(c, c))
    for term in name_terms or []:
        phrases.extend(_expand_name_term(term))
    if not phrases:
        phrases = ["business"]

    seen = set()
    out: List[str] = []
    for p in phrases:
        key = p.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(p.strip())
    return out


def satellite_points(
    lat: float, lon: float, current_radius_km: float,
) -> List[Tuple[float, float, float]]:
    """Ring of (lat, lon, radius_km) around the centre for suburb-scale crawls."""
    ring_km = max(1.0, current_radius_km * SATELLITE_RING_RADIUS_FACTOR)
    point_radius = max(1.0, current_radius_km * SATELLITE_POINT_RADIUS_FACTOR)
    n = max(1, SATELLITE_RING_POINTS)
    cos_lat = math.cos(math.radians(lat)) or 1e-6
    points: List[Tuple[float, float, float]] = []
    for i in range(n):
        angle = 2 * math.pi * i / n
        dlat = (ring_km * math.cos(angle)) / 111.0
        dlon = (ring_km * math.sin(angle)) / (111.0 * cos_lat)
        points.append((lat + dlat, lon + dlon, point_radius))
    return points


def _row_address(item: Dict) -> str:
    if item.get("address"):
        return item["address"]
    parts = [item.get("street", ""), item.get("city", ""), item.get("postalCode", "")]
    return " ".join(p for p in parts if p).strip()


def _extract_row(item: Dict) -> Optional[Dict]:
    name = item.get("title")
    if not name:
        return None
    if item.get("permanentlyClosed") or item.get("temporarilyClosed"):
        return None

    address = _row_address(item)
    if chain_filter.reject_reason(name, address):
        return None

    location = item.get("location") or {}
    place_id = item.get("placeId") or item.get("cid") or name
    hours = opening_hours.parse_opening_hours(
        item.get("openingHours") or item.get("opening_hours")
    )

    return {
        "business_name": name,
        "address": address,
        "phone": item.get("phone") or item.get("phoneUnformatted") or "",
        "email": "",
        "website": item.get("website") or "",
        "operator": "",
        "category": item.get("categoryName") or "",
        "lat": location.get("lat"),
        "lon": location.get("lng"),
        "osm_id": f"gmaps/{place_id}",
        "opening_hours": hours,
        "opening_hours_display": opening_hours.format_opening_hours_display(hours),
    }


def bbox_to_polygon(bbox: Sequence[float]) -> Dict:
    """Nominatim [south, north, west, east] → GeoJSON Polygon (lon, lat rings)."""
    south, north, west, east = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    # Closed ring: SW → SE → NE → NW → SW
    ring = [
        [west, south],
        [east, south],
        [east, north],
        [west, north],
        [west, south],
    ]
    return {"type": "Polygon", "coordinates": [ring]}


def _start_run(
    search_strings: List[str],
    place: str,
    per_search_cap: int,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius_km: Optional[float] = None,
    bbox: Optional[Sequence[float]] = None,
) -> Dict:
    payload: Dict = {
        "searchStringsArray": search_strings,
        "locationQuery": place,
        "maxCrawledPlacesPerSearch": per_search_cap,
        "language": "en",
        "skipClosedPlaces": True,
        "scrapeContacts": False,
        # Needed for weekly openingHours used by dial-window CSV splits.
        "scrapePlaceDetailPage": True,
    }
    geo_mode = "locationQuery_only"
    # Prefer place footprint (bbox polygon) when provided; else point+radius.
    if bbox is not None and len(bbox) >= 4:
        payload["customGeolocation"] = bbox_to_polygon(bbox)
        geo_mode = "bbox"
    elif lat is not None and lon is not None and radius_km and radius_km > 0:
        # GeoJSON Point coordinates are [longitude, latitude].
        payload["customGeolocation"] = {
            "type": "Point",
            "coordinates": [lon, lat],
            "radiusKm": float(radius_km),
        }
        geo_mode = "point+radius"

    try:
        resp = requests.post(
            RUNS_URL,
            params={"timeout": APIFY_ACTOR_RUN_TIMEOUT_SECONDS},
            headers=_headers(),
            json=payload,
            timeout=APIFY_HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise ApifyError(f"Couldn't reach Apify: {exc}") from exc

    if resp.status_code == 401:
        raise ApifyError("Apify rejected the API token — check APIFY_API_TOKEN in backend/.env.")
    if resp.status_code not in (200, 201):
        raise ApifyError(f"Apify couldn't start the crawl (HTTP {resp.status_code}).")

    run = resp.json().get("data", {})
    if not run.get("id") or not run.get("defaultDatasetId"):
        raise ApifyError("Apify's response didn't include a run id — can't continue.")
    run["_geo_mode"] = geo_mode
    return run


def _abort_run(run_id: str) -> None:
    try:
        requests.post(
            RUN_ABORT_URL.format(run_id=run_id),
            headers=_headers(),
            timeout=APIFY_HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        pass


def _poll_until_done(run: Dict, cancel_event, on_progress: Optional[Callable[[float], None]]) -> str:
    run_id = run["id"]
    status = run.get("status", "READY")
    waited = 0.0

    while status not in _TERMINAL_STATUSES:
        if cancel_event is not None and cancel_event.is_set():
            _abort_run(run_id)
            return "ABORTED"
        if waited >= APIFY_MAX_WAIT_SECONDS:
            _abort_run(run_id)
            raise ApifyError(
                f"Apify crawl didn't finish within {APIFY_MAX_WAIT_SECONDS}s. "
                "Try a smaller area, fewer business types, or a lower lead target."
            )

        time.sleep(APIFY_POLL_INTERVAL_SECONDS)
        waited += APIFY_POLL_INTERVAL_SECONDS
        if on_progress:
            on_progress(waited)

        try:
            resp = requests.get(
                RUN_STATUS_URL.format(run_id=run_id),
                headers=_headers(),
                timeout=APIFY_HTTP_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            status = resp.json().get("data", {}).get("status", status)
        except requests.RequestException:
            continue

    return status


def _fetch_dataset_items(dataset_id: str) -> List[Dict]:
    try:
        resp = requests.get(
            DATASET_ITEMS_URL.format(dataset_id=dataset_id),
            params={"clean": "true"},
            headers=_headers(),
            timeout=APIFY_HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise ApifyError(f"Couldn't fetch results from Apify: {exc}") from exc


def search(
    place: str,
    categories: List[str],
    name_terms: List[str],
    target_leads: int,
    cancel_event=None,
    on_progress: Optional[Callable[[float], None]] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius_km: Optional[float] = None,
    search_strings: Optional[Sequence[str]] = None,
    bbox: Optional[Sequence[float]] = None,
) -> Dict:
    """One Apify crawl for the given phrases + place/geo.

    Prefer `bbox` (place footprint) when set; otherwise Point + radiusKm.
    Returns rows plus funnel counts for job-log diagnostics.
    """
    if not APIFY_API_TOKEN:
        raise ApifyError(
            "No Apify API token configured. Add APIFY_API_TOKEN to backend/.env "
            "to use the Google Maps data source."
        )

    phrases = list(search_strings) if search_strings is not None else build_search_phrases(
        categories, name_terms
    )
    if not phrases:
        phrases = ["business"]

    n_strings = max(1, len(phrases))
    per_search_cap = max(20, -(-target_leads // n_strings))

    run = _start_run(
        phrases, place, per_search_cap,
        lat=lat, lon=lon, radius_km=radius_km, bbox=bbox,
    )
    geo_mode = run.pop("_geo_mode", "unknown")
    status = _poll_until_done(run, cancel_event, on_progress)

    if status == "FAILED":
        raise ApifyError(
            "The Apify Google Maps crawl failed. Check the run in the Apify console for details."
        )
    if status == "TIMED-OUT":
        raise ApifyError(
            f"Apify's actor hit its own {APIFY_ACTOR_RUN_TIMEOUT_SECONDS}s run timeout. "
            "Try a smaller area, fewer business types, or a lower lead target."
        )

    items = _fetch_dataset_items(run["defaultDatasetId"])

    rows: List[Dict] = []
    seen_ids = set()
    for item in items:
        row = _extract_row(item)
        if row and row["osm_id"] not in seen_ids:
            seen_ids.add(row["osm_id"])
            rows.append(row)
    return {
        "rows": rows,
        "raw_items": len(items),
        "after_extract": len(rows),
        "search_strings": n_strings,
        "search_phrases": phrases,
        "per_search_cap": per_search_cap,
        "radius_km": radius_km,
        "geo_mode": geo_mode,
    }
