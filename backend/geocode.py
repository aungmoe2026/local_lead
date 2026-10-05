"""
Geocode a place name (e.g. "Ruislip, UK" or "Chicago, IL") into a rich place
result using the free Nominatim API. No key required.

Returns lat/lon plus bounding box and address parts so Maps discovery can use
the real place footprint and optionally expand to a parent county.
"""

import time
from typing import Any, Dict, Optional, Tuple

import requests

from config import NOMINATIM_URL, USER_AGENT, REQUEST_TIMEOUT_SECONDS, NOMINATIM_DELAY_SECONDS

# Nominatim addresstype / type values that already mean "county-scale".
_COUNTY_LEVEL_TYPES = {
    "county",
    "state",
    "state_district",
    "region",
    "province",
    "administrative",
}


class GeocodeError(Exception):
    """Raised when Nominatim cannot resolve a place name."""


def _parse_bbox(raw: Any) -> Optional[Tuple[float, float, float, float]]:
    """Nominatim boundingbox: [south, north, west, east] as strings."""
    if not raw or len(raw) < 4:
        return None
    try:
        south, north, west, east = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
    except (TypeError, ValueError):
        return None
    if south >= north or west == east:
        return None
    return (south, north, west, east)


def parent_area_query(place: Dict[str, Any]) -> Optional[str]:
    """Build a Nominatim/Google query for the parent county/region, or None."""
    addr = place.get("address") or {}
    if not isinstance(addr, dict):
        return None

    addresstype = (place.get("addresstype") or place.get("place_type") or "").lower()
    # If the user already searched a county/state, don't expand further.
    if addresstype in _COUNTY_LEVEL_TYPES:
        return None
    osm_type = (place.get("type") or "").lower()
    if osm_type in ("county", "state", "state_district", "region"):
        return None

    parent = (
        addr.get("county")
        or addr.get("state_district")
        or addr.get("state")
        or ""
    ).strip()
    if not parent:
        return None

    country = (addr.get("country") or "").strip()
    first = (place.get("display_name") or "").split(",")[0].strip().lower()
    if parent.lower() == first:
        return None

    if country:
        return f"{parent}, {country}"
    return parent


def geocode(place: str) -> Dict[str, Any]:
    """Resolve a place name. Raises GeocodeError on failure.

    Returns dict with: lat, lon, display_name, boundingbox (optional),
    address (optional), addresstype, type, parent_query (optional).
    """
    place = (place or "").strip()
    if not place:
        raise GeocodeError("Place is required")

    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={
                "q": place,
                "format": "json",
                "limit": 1,
                "addressdetails": 1,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        results = resp.json()
    except requests.RequestException as exc:
        raise GeocodeError(
            f"Could not reach the geocoding service for '{place}'. "
            f"Check your internet connection and try again. ({exc})"
        ) from exc
    finally:
        # Always honour Nominatim's 1 req/s policy, even on failure.
        time.sleep(NOMINATIM_DELAY_SECONDS)

    if not results:
        raise GeocodeError(
            f"Could not find a location for '{place}'. "
            f"Try being more specific (e.g. add the country: '{place}, UK')."
        )

    top = results[0]
    address = top.get("address") if isinstance(top.get("address"), dict) else {}
    result: Dict[str, Any] = {
        "lat": float(top["lat"]),
        "lon": float(top["lon"]),
        "display_name": top.get("display_name", place),
        "address": address,
        "addresstype": top.get("addresstype") or "",
        "type": top.get("type") or "",
        "boundingbox": None,
        "parent_query": None,
    }
    bbox = _parse_bbox(top.get("boundingbox"))
    if bbox:
        result["boundingbox"] = list(bbox)

    result["parent_query"] = parent_area_query(result)
    return result
