"""
Query the free Overpass API (OpenStreetMap) for businesses of chosen
categories within a radius of a lat/lon point. No key required.

Handles two matching modes, which can be combined:
  - Tag-based:  category names from config.CATEGORIES (e.g. "cafe", "pub")
  - Name-based: free-text name match (e.g. "Conservative Club",
                "Working Men's Club") for things OSM doesn't tag consistently.

Tries several Overpass mirrors with short retries so temporary 504/overload
doesn't kill a whole lead run.
"""

import time
from typing import Dict, List, Optional

import requests

from config import CATEGORIES, OVERPASS_URLS, REQUEST_TIMEOUT_SECONDS, USER_AGENT


class OverpassError(Exception):
    """Raised when every Overpass mirror fails."""


def _build_query(lat: float, lon: float, radius_m: int,
                  categories: List[str], name_terms: List[str],
                  timeout_s: int = 60) -> str:
    # nwr = node+way+relation in one clause (faster than duplicating node/way)
    clauses = []

    for cat in categories:
        tag_pairs = CATEGORIES.get(cat, [])
        for key, value in tag_pairs:
            selector = f'"{key}"' if value == "*" else f'"{key}"="{value}"'
            clauses.append(f'nwr[{selector}](around:{radius_m},{lat},{lon});')

    for term in name_terms:
        escaped = term.replace('"', '\\"')
        clauses.append(f'nwr["name"~"{escaped}",i](around:{radius_m},{lat},{lon});')

    body = "\n  ".join(clauses)
    return f"""
[out:json][timeout:{timeout_s}];
(
  {body}
);
out center tags;
""".strip()


def _tags_to_address(tags: Dict[str, str]) -> str:
    parts = [
        tags.get("addr:housenumber", ""),
        tags.get("addr:street", ""),
        tags.get("addr:city", "") or tags.get("addr:town", "") or tags.get("addr:village", ""),
        tags.get("addr:postcode", ""),
    ]
    return " ".join(p for p in parts if p).strip()


def _category_label(tags: Dict[str, str], matched_categories: List[str]) -> str:
    """Prefer the user's selected category name when we can map it; else OSM tag."""
    amenity = tags.get("amenity", "")
    tourism = tags.get("tourism", "")
    shop = tags.get("shop", "")
    leisure = tags.get("leisure", "")

    osm_to_cat = {
        "cafe": "cafe",
        "restaurant": "restaurant",
        "pub": "pub",
        "bar": "bar",
        "fast_food": "fast_food",
        "guest_house": "bnb",
        "hotel": "hotel",
        "social_centre": "club",
        "fitness_centre": "gym",
        "sports_centre": "gym",
        "hairdresser": "salon",
        "beauty": "salon",
    }
    for raw in (amenity, tourism, shop, leisure):
        mapped = osm_to_cat.get(raw)
        if mapped and (not matched_categories or mapped in matched_categories):
            return mapped
        if mapped:
            return mapped
    if tags.get("club") and "club" in matched_categories:
        return "club"
    if shop and "shop" in matched_categories:
        return "shop"
    return amenity or tourism or shop or leisure or tags.get("club") or ""


def _extract_row(element: Dict, matched_categories: List[str]) -> Optional[Dict]:
    tags = element.get("tags", {})
    name = tags.get("name")
    if not name:
        return None  # skip unnamed nodes, not useful as leads

    if tags.get("brand") or tags.get("brand:wikidata"):
        return None  # franchise/chain location, tagged as such by OSM — not a small-business lead

    if element["type"] == "node":
        lat, lon = element.get("lat"), element.get("lon")
    else:
        center = element.get("center", {})
        lat, lon = center.get("lat"), center.get("lon")

    website = (
        tags.get("website")
        or tags.get("contact:website")
        or tags.get("url")
        or ""
    )
    phone = (
        tags.get("phone")
        or tags.get("contact:phone")
        or tags.get("contact:mobile")
        or ""
    )
    email = tags.get("email") or tags.get("contact:email") or ""
    # OSM sometimes stores a person/company name — useful when websites fail.
    operator = (
        tags.get("owner")
        or tags.get("contact:name")
        or tags.get("operator")
        or ""
    )

    return {
        "business_name": name,
        "address": _tags_to_address(tags),
        "phone": phone,
        "email": email,
        "website": website,
        "operator": operator,
        "category": _category_label(tags, matched_categories),
        "lat": lat,
        "lon": lon,
        "osm_id": f"{element['type']}/{element['id']}",
    }


def _post_query(url: str, query: str, http_timeout: int) -> List[Dict]:
    resp = requests.post(
        url,
        data={"data": query},
        headers={"User-Agent": USER_AGENT},
        timeout=http_timeout,
    )
    if resp.status_code in (429, 502, 503, 504):
        raise OverpassError(f"{url} returned {resp.status_code}")
    resp.raise_for_status()
    return resp.json().get("elements", [])


def search(lat: float, lon: float, radius_m: int,
           categories: List[str], name_terms: List[str],
           timeout_s: int = 60) -> List[Dict]:
    if not categories and not name_terms:
        return []

    # Cap Overpass server-side timeout a bit lower so we fail over faster
    server_timeout = min(timeout_s, 40)
    query = _build_query(
        lat, lon, radius_m, categories, name_terms, timeout_s=server_timeout
    )
    http_timeout = max(REQUEST_TIMEOUT_SECONDS, server_timeout + 15)
    last_error: Optional[Exception] = None

    # Round-robin mirrors; 2 full passes with backoff between passes.
    for attempt in range(2):
        if attempt > 0:
            time.sleep(2.5 * attempt)

        for url in OVERPASS_URLS:
            try:
                elements = _post_query(url, query, http_timeout)
                rows = []
                seen_ids = set()
                for el in elements:
                    row = _extract_row(el, categories)
                    if row and row["osm_id"] not in seen_ids:
                        seen_ids.add(row["osm_id"])
                        rows.append(row)
                return rows
            except (requests.RequestException, ValueError, OverpassError) as exc:
                last_error = exc
                continue

    raise OverpassError(
        f"All Overpass mirrors failed. Last error: {last_error}. "
        "The free map servers are busy — wait 30 seconds and try again."
    )
