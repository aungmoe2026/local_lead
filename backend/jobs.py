"""
Job manager. Each "Generate Leads" click creates one job that runs in a
background thread so the API can return immediately and the frontend polls.

Jobs live in memory and are snapshotted to outputs/jobs/{id}.json so a
uvicorn --reload restart can recover finished jobs (or mark mid-run jobs as
interrupted). Still single-process / single-user; swap for a real queue if
you ever need multi-user.
"""

import csv
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from config import (
    DEFAULT_TARGET_LEADS,
    ENRICHMENT_WORKERS,
    MAX_RADIUS_EXPANSIONS,
    MAX_RADIUS_KM,
    PREVIEW_ROW_LIMIT,
    RADIUS_EXPANSION_FACTOR,
    COMPANIES_HOUSE_API_KEY,
    TARGET_LEAD_OVERAGE,
)
from estimate import estimate_job_seconds
from geocode import geocode
from overpass_search import search
from contact_enricher import enrich_listing
import apify_maps
import chain_filter
import companies_house
import opening_hours

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "outputs")
JOBS_DIR = os.path.join(OUTPUT_DIR, "jobs")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(JOBS_DIR, exist_ok=True)

FIELDNAMES = [
    "Business Name", "Owner / Partner Name", "Business Address",
    "Phone", "Email", "Website", "Category", "Owner Evidence",
    "Owner Source", "Trading Address", "CH Registered Office",
    "Company Number", "Address Mismatch", "Opening Hours",
]

# Columns written to downloadable CSVs only (UI still uses full lead objects).
# Matches MaxContact / dialler import layout from the CRM.
EXPORT_FIELDNAMES = [
    "Contact_Title",
    "Contact_Firstnames",
    "Contact_Surname",
    "Business_Name",
    "address",
    "postcode",
    "Company_Telephone",
    "Contact_Telephone",
    "Contact_Mobile",
    "SIC_Code",
    "SIC_Description",
]

_UK_POSTCODE_RE = re.compile(
    r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b",
    re.IGNORECASE,
)

JOBS: Dict[str, Dict] = {}
_CANCEL_EVENTS: Dict[str, threading.Event] = {}
_lock = threading.Lock()

_INTERRUPTED_MSG = "Server restarted mid-job. Start a new search."


def _job_path(job_id: str) -> str:
    return os.path.join(JOBS_DIR, f"{job_id}.json")


def _persist_unlocked(job_id: str) -> None:
    """Best-effort snapshot to disk. Never raise — Windows often denies
    os.replace briefly while Defender/Cursor/OneDrive has the .json open,
    and that must not fail the whole lead job."""
    job = JOBS.get(job_id)
    if not job:
        return
    path = _job_path(job_id)
    tmp = path + ".tmp"
    payload = json.dumps(job, ensure_ascii=False, default=str)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        return

    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            # WinError 5 / errno 13: destination briefly locked by another reader.
            time.sleep(0.05 * (attempt + 1))

    # Fallback: overwrite in place (less atomic, but keeps the job alive).
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
    except OSError:
        pass
    try:
        os.remove(tmp)
    except OSError:
        pass


def _update(job_id: str, **fields):
    with _lock:
        JOBS[job_id].update(fields)
        _persist_unlocked(job_id)


def _log(job_id: str, message: str):
    with _lock:
        JOBS[job_id]["logs"].append(message)
        _persist_unlocked(job_id)


def _attach_hours_fields(result: Dict, listing: Dict) -> Dict:
    """Copy opening hours from discovery listing onto a CSV/preview row."""
    hours = listing.get("opening_hours") or {}
    if not isinstance(hours, dict):
        hours = opening_hours.parse_opening_hours(hours)
    display = listing.get("opening_hours_display") or opening_hours.format_opening_hours_display(hours)
    slots = opening_hours.matching_dial_slots(hours)
    result["Opening Hours"] = display
    result["opening_hours"] = hours
    result["opening_hours_display"] = display
    result["dial_slots"] = slots
    return result


def _push_preview(job_id: str, row: Dict):
    """Append finished row to full leads list; keep a short preview sample."""
    with _lock:
        leads = JOBS[job_id].setdefault("leads", [])
        leads.append(row)
        preview = JOBS[job_id]["preview"]
        if len(preview) < PREVIEW_ROW_LIMIT:
            preview.append(row)
        _persist_unlocked(job_id)


def stop_job(job_id: str) -> bool:
    """Requests a running job to stop. It finishes up gracefully - whatever
    was already found/processed gets saved to CSV rather than thrown away."""
    event = _CANCEL_EVENTS.get(job_id)
    if not event:
        return False
    event.set()
    _update(job_id, status="stopping")
    _log(job_id, "Stop requested - finishing up and saving what's been found so far...")
    return True


def create_job(place: str, radius_km: float, categories: List[str],
               name_terms: List[str], enrich: bool,
               target_leads: int = DEFAULT_TARGET_LEADS,
               include_county: bool = True) -> str:
    job_id = uuid.uuid4().hex[:12]
    eta_low, eta_high, eta_label = estimate_job_seconds(
        categories, name_terms, target_leads, enrich,
        include_county=include_county,
    )
    with _lock:
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",       # queued -> running -> stopping -> done | stopped | error
            "progress": 0,
            "total": 0,
            "logs": [],
            "error": None,
            "csv_path": None,
            "result_count": 0,
            "owners_found": 0,
            "phones_found": 0,
            "emails_found": 0,
            "websites_found": 0,
            "preview": [],
            "leads": [],
            "dial_slot_counts": {sid: 0 for sid in opening_hours.DIAL_SLOTS},
            "place": place,
            "created_at": time.time(),
            "eta_seconds_low": eta_low,
            "eta_seconds_high": eta_high,
            "eta_label": eta_label,
            "include_county": include_county,
        }
        _persist_unlocked(job_id)
    _CANCEL_EVENTS[job_id] = threading.Event()
    thread = threading.Thread(
        target=_run_job,
        args=(job_id, place, radius_km, categories, name_terms, enrich,
              target_leads, include_county),
        daemon=True,
    )
    thread.start()
    return job_id


def get_job(job_id: str) -> Optional[Dict]:
    with _lock:
        job = JOBS.get(job_id)
        if job is not None:
            return job

    path = _job_path(job_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            job = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(job, dict):
        return None

    if job.get("status") in ("queued", "running", "stopping"):
        job["status"] = "error"
        job["error"] = _INTERRUPTED_MSG
        logs = job.get("logs")
        if not isinstance(logs, list):
            logs = []
        logs.append(_INTERRUPTED_MSG)
        job["logs"] = logs

    with _lock:
        # Another request may have loaded it first.
        existing = JOBS.get(job_id)
        if existing is not None:
            return existing
        JOBS[job_id] = job
        _persist_unlocked(job_id)
        return job


def _normalize_key(name: str, address: str) -> str:
    """Cheap dedupe key so the same cafe listed twice doesn't pad the CSV."""
    blob = f"{name}|{address}".lower()
    return re.sub(r"[^a-z0-9|]+", "", blob)


def _search_with_expansion(job_id: str, lat: float, lon: float, radius_km: float,
                            categories: List[str], name_terms: List[str],
                            target_leads: int, cancel_event: threading.Event) -> List[Dict]:
    """
    Run the Overpass search, widening the radius automatically if the first
    pass comes back short of target_leads. Overpass has no per-query results
    cap (unlike Google Places), so simply searching a bigger area is enough -
    no ZIP/grid fan-out needed.
    """
    current_radius = radius_km
    rows: List[Dict] = []

    for attempt in range(MAX_RADIUS_EXPANSIONS + 1):
        if cancel_event.is_set():
            _log(job_id, "Stopped before search finished - using what was found so far.")
            break

        timeout_s = min(35 + attempt * 10, 60)
        _log(job_id, f"Searching within {current_radius:.1f} km...")
        radius_m = int(current_radius * 1000)
        try:
            new_rows = search(lat, lon, radius_m, categories, name_terms, timeout_s=timeout_s)
        except Exception as exc:
            # One soft retry after a pause — Overpass 504s are often brief.
            _log(job_id, f"  Map server busy ({exc}). Retrying in 4s...")
            time.sleep(4)
            new_rows = search(lat, lon, radius_m, categories, name_terms, timeout_s=timeout_s)

        rows = []
        seen_ids = set()
        seen_keys = set()
        for r in new_rows:
            if r["osm_id"] in seen_ids:
                continue
            key = _normalize_key(r.get("business_name", ""), r.get("address", ""))
            if key in seen_keys:
                continue
            seen_ids.add(r["osm_id"])
            seen_keys.add(key)
            rows.append(r)

        rows = _apply_lead_quality_filters(job_id, rows)
        _log(job_id, f"  -> {len(rows)} unique listings so far.")

        if len(rows) >= target_leads:
            break
        if current_radius >= MAX_RADIUS_KM:
            _log(job_id, f"Reached the {MAX_RADIUS_KM} km search limit; using what's available.")
            break
        if attempt == MAX_RADIUS_EXPANSIONS:
            break

        next_radius = min(current_radius * RADIUS_EXPANSION_FACTOR, MAX_RADIUS_KM)
        _log(job_id, f"Only {len(rows)} found so far (target {target_leads}) - "
                     f"widening search to {next_radius:.1f} km...")
        current_radius = next_radius

    return _sort_and_trim(job_id, rows, target_leads)


def _apply_lead_quality_filters(job_id: str, rows: List[Dict]) -> List[Dict]:
    """Drops chains, head offices, and retail-park sites (high-street focus)."""
    before = len(rows)
    rows, counts = chain_filter.filter_low_quality_leads(rows)
    removed = before - len(rows)
    if removed:
        parts = []
        if counts.get("chain"):
            parts.append(f"{counts['chain']} chain/franchise")
        if counts.get("head_office"):
            parts.append(f"{counts['head_office']} head office")
        if counts.get("retail_park"):
            parts.append(f"{counts['retail_park']} retail park/mall")
        detail = ", ".join(parts) if parts else f"{removed} listings"
        _log(
            job_id,
            f"  Filtered out {removed} listing(s) ({detail}) "
            f"— focusing on independent high-street businesses.",
        )
    return rows


def _sort_and_trim(job_id: str, rows: List[Dict], target_leads: int) -> List[Dict]:
    # Prefer listings that already have a website / phone — better enrichment yield
    # and more useful for outreach. Stable sort keeps relative source order within tiers.
    def _quality(r: Dict) -> tuple:
        return (
            0 if r.get("website") else 1,
            0 if r.get("phone") or r.get("email") else 1,
            r.get("business_name", "").lower(),
        )

    rows.sort(key=_quality)
    cap = target_leads + TARGET_LEAD_OVERAGE
    if len(rows) > cap:
        _log(
            job_id,
            f"Trimming {len(rows)} listings down to {cap} "
            f"(target {target_leads} + {TARGET_LEAD_OVERAGE} overage; "
            f"websites/phones first).",
        )
        rows = rows[:cap]

    return rows


def _basic_listing_row(row: Dict, evidence: str) -> Dict:
    """CSV/preview row for a listing that was not fully enriched."""
    trading = row.get("address", "") or ""
    result = {
        "Business Name": row.get("business_name", ""),
        "Owner / Partner Name": "",
        "Business Address": trading,
        "Phone": row.get("phone", ""), "Email": row.get("email", ""),
        "Website": row.get("website", ""), "Category": row.get("category", ""),
        "Owner Evidence": evidence,
        "Owner Source": "none",
        "Trading Address": trading,
        "CH Registered Office": "",
        "Company Number": "",
        "Address Mismatch": "no",
        "owner_source": "none",
        "owner_evidence": evidence,
        "trading_address": trading,
        "ch_registered_office": "",
        "company_number": "",
        "address_mismatch": False,
        "lat": row.get("lat"),
        "lon": row.get("lon"),
    }
    return _attach_hours_fields(result, row)


def _split_owner_name(owner: str) -> tuple:
    """Split 'Owner / Partner Name' into (First name, Surname)."""
    parts = (owner or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _extract_uk_postcode(address: str) -> tuple:
    """Return (address without postcode, normalised postcode)."""
    text = (address or "").strip()
    if not text:
        return "", ""
    m = _UK_POSTCODE_RE.search(text)
    if not m:
        return text, ""
    raw = re.sub(r"\s+", " ", m.group(1)).strip().upper()
    # Standard spacing: outward + space + inward (last 3 chars)
    if " " not in raw and len(raw) >= 5:
        postcode = f"{raw[:-3]} {raw[-3:]}"
    else:
        postcode = raw
    cleaned = (text[: m.start()] + text[m.end() :]).strip()
    cleaned = re.sub(r"[\s,;]+$", "", cleaned).strip(" ,;")
    return cleaned, postcode


def to_export_row(row: Dict) -> Dict:
    """Map a full UI lead row to the MaxContact download CSV shape."""
    trading = (
        row.get("Trading Address")
        or row.get("trading_address")
        or row.get("Business Address")
        or ""
    )
    address, postcode = _extract_uk_postcode(trading)
    first, surname = _split_owner_name(row.get("Owner / Partner Name") or "")
    phone = (row.get("Phone") or "").strip()
    sic_code = (
        row.get("SIC_Code")
        or row.get("sic_code")
        or ""
    )
    if isinstance(sic_code, list):
        sic_code = (sic_code[0] if sic_code else "") or ""
    sic_description = (
        row.get("SIC_Description")
        or row.get("sic_description")
        or row.get("Category")
        or ""
    )
    return {
        "Contact_Title": "",
        "Contact_Firstnames": first,
        "Contact_Surname": surname,
        "Business_Name": row.get("Business Name") or "",
        "address": address,
        "postcode": postcode,
        # Business line from Maps / enrichment — dialler company phone
        "Company_Telephone": phone,
        "Contact_Telephone": "",
        "Contact_Mobile": "",
        "SIC_Code": str(sic_code).strip(),
        "SIC_Description": str(sic_description).strip(),
    }


def _write_export_csv(path: str, rows: List[Dict]) -> str:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=EXPORT_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(to_export_row(r) for r in rows)
    return path


def _write_job_csv(job_id: str, rows: List[Dict]) -> str:
    """Write slim export CSV to outputs/{job_id}.csv and return the path."""
    csv_path = os.path.join(OUTPUT_DIR, f"{job_id}.csv")
    return _write_export_csv(csv_path, rows)


def write_slot_csv(job_id: str, slot_id: str) -> Optional[str]:
    """Write a dial-window filtered export CSV; return path or None if empty/invalid."""
    if slot_id not in opening_hours.DIAL_SLOTS:
        return None
    job = get_job(job_id)
    if not job:
        return None
    leads = job.get("leads") or job.get("preview") or []
    filtered = [r for r in leads if opening_hours.lead_matches_slot(r, slot_id)]
    if not filtered:
        return None
    csv_path = os.path.join(OUTPUT_DIR, f"{job_id}_{slot_id}.csv")
    return _write_export_csv(csv_path, filtered)


def write_all_csv(job_id: str) -> Optional[str]:
    """Regenerate the full export CSV from current leads (for download)."""
    job = get_job(job_id)
    if not job:
        return None
    leads = job.get("leads") or job.get("preview") or []
    if not leads:
        return None
    csv_path = os.path.join(OUTPUT_DIR, f"{job_id}.csv")
    return _write_export_csv(csv_path, leads)


def _salvage_csv_rows(
    listings: List[Dict],
    final_rows: List[Optional[Dict]],
    evidence: str,
) -> List[Dict]:
    """Merge completed enrichments with stubs for listings not finished yet."""
    out: List[Dict] = []
    for i, listing in enumerate(listings):
        if i < len(final_rows) and final_rows[i] is not None:
            out.append(final_rows[i])  # type: ignore[arg-type]
        else:
            out.append(_basic_listing_row(listing, evidence))
    return out


def _merge_unique_rows(into: List[Dict], seen_ids: set, new_rows: List[Dict]) -> int:
    """Append new rows not already in seen_ids. Returns how many were added."""
    added = 0
    for row in new_rows:
        oid = row.get("osm_id")
        if not oid or oid in seen_ids:
            continue
        seen_ids.add(oid)
        into.append(row)
        added += 1
    return added


def _discover_maps_leads(
    job_id: str,
    place: str,
    display_name: str,
    lat: float,
    lon: float,
    radius_km: float,
    categories: List[str],
    name_terms: List[str],
    target_leads: int,
    cancel_event: threading.Event,
    bbox: Optional[List[float]] = None,
    parent_query: Optional[str] = None,
    include_county: bool = True,
) -> List[Dict]:
    """Wider Maps discovery: phrases → bbox/radius → satellite → county."""
    phrases = apify_maps.build_search_phrases(categories, name_terms)
    _log(
        job_id,
        f"  Search phrases ({len(phrases)}): " + ", ".join(phrases[:12])
        + ("…" if len(phrases) > 12 else ""),
    )
    if bbox:
        _log(job_id, "  Place footprint (bounding box) available — first crawl uses bbox.")

    def _on_progress(waited: float, _job_id=job_id):
        if waited % 20 == 0:
            _log(_job_id, f"  Still running on Apify... {int(waited)}s elapsed.")

    rows: List[Dict] = []
    seen_ids: set = set()
    current_radius = float(radius_km) if radius_km > 0 else 12.0
    place_bbox = list(bbox) if bbox and len(bbox) >= 4 else None

    for attempt in range(MAX_RADIUS_EXPANSIONS + 1):
        if cancel_event.is_set():
            break
        remaining = max(1, target_leads - len(rows))
        # First pass: prefer bbox footprint. Later expands use point+radius.
        use_bbox = place_bbox if attempt == 0 else None
        mode_note = "bbox" if use_bbox else f"{current_radius:.1f} km"
        _log(
            job_id,
            f"  Maps crawl @ {mode_note} "
            f"(need ~{remaining} more toward {target_leads})...",
        )
        discovery = apify_maps.search(
            display_name,
            categories,
            name_terms,
            remaining,
            cancel_event=cancel_event,
            on_progress=_on_progress,
            lat=lat,
            lon=lon,
            radius_km=current_radius,
            search_strings=phrases,
            bbox=use_bbox,
        )
        raw_items = discovery.get("raw_items", 0)
        after_extract = discovery.get("after_extract", 0)
        n_strings = discovery.get("search_strings", 1)
        per_cap = discovery.get("per_search_cap", 0)
        geo_mode = discovery.get("geo_mode", "?")
        batch = discovery.get("rows") or []
        added = _merge_unique_rows(rows, seen_ids, batch)
        _log(
            job_id,
            f"  Apify funnel ({geo_mode}): {raw_items} raw → "
            f"{after_extract} kept → +{added} new "
            f"(cumulative {len(rows)}; {n_strings} search(es) × up to {per_cap}).",
        )

        if len(rows) >= target_leads or cancel_event.is_set():
            break
        if current_radius >= MAX_RADIUS_KM and attempt > 0:
            _log(job_id, f"  Reached {MAX_RADIUS_KM} km radius limit.")
            break
        if attempt == MAX_RADIUS_EXPANSIONS:
            break

        next_radius = min(current_radius * RADIUS_EXPANSION_FACTOR, MAX_RADIUS_KM)
        if next_radius <= current_radius and attempt > 0:
            break
        _log(
            job_id,
            f"  Only {len(rows)} unique so far (target {target_leads}) — "
            f"widening search to {next_radius:.1f} km...",
        )
        current_radius = next_radius

    # Layer 3: satellite ring if still short after radius expansion.
    if (
        len(rows) < target_leads
        and not cancel_event.is_set()
    ):
        sats = apify_maps.satellite_points(lat, lon, current_radius)
        _log(
            job_id,
            f"  Still short of target — satellite ring of {len(sats)} "
            f"offset crawl(s) around {place}...",
        )
        for i, (slat, slon, sradius) in enumerate(sats, start=1):
            if cancel_event.is_set() or len(rows) >= target_leads:
                break
            remaining = max(1, target_leads - len(rows))
            _log(
                job_id,
                f"  Satellite {i}/{len(sats)} @ {sradius:.1f} km "
                f"(need ~{remaining} more)...",
            )
            discovery = apify_maps.search(
                display_name,
                categories,
                name_terms,
                remaining,
                cancel_event=cancel_event,
                on_progress=_on_progress,
                lat=slat,
                lon=slon,
                radius_km=sradius,
                search_strings=phrases,
            )
            batch = discovery.get("rows") or []
            added = _merge_unique_rows(rows, seen_ids, batch)
            _log(
                job_id,
                f"  Satellite {i}: +{added} new "
                f"(cumulative {len(rows)} unique).",
            )

    # Layer 4: parent county / wider admin area when still short.
    if (
        include_county
        and parent_query
        and len(rows) < target_leads
        and not cancel_event.is_set()
    ):
        _log(job_id, f"  Still short — looking up surrounding area '{parent_query}'...")
        try:
            parent = geocode(parent_query)
        except Exception as exc:  # noqa: BLE001
            _log(job_id, f"  Could not resolve parent area ({exc}); skipping county pass.")
            parent = None

        if parent:
            remaining = max(1, target_leads - len(rows))
            parent_name = parent.get("display_name") or parent_query
            parent_bbox = parent.get("boundingbox")
            _log(
                job_id,
                f"  County/wider pass: {parent_name} "
                f"(need ~{remaining} more)...",
            )
            discovery = apify_maps.search(
                parent_name,
                categories,
                name_terms,
                remaining,
                cancel_event=cancel_event,
                on_progress=_on_progress,
                lat=parent["lat"],
                lon=parent["lon"],
                radius_km=current_radius,
                search_strings=phrases,
                bbox=parent_bbox if parent_bbox else None,
            )
            batch = discovery.get("rows") or []
            added = _merge_unique_rows(rows, seen_ids, batch)
            _log(
                job_id,
                f"  County pass: +{added} new "
                f"(cumulative {len(rows)} unique; geo={discovery.get('geo_mode')}).",
            )

    return rows


def _run_job(job_id: str, place: str, radius_km: float,
             categories: List[str], name_terms: List[str], enrich: bool,
             target_leads: int = DEFAULT_TARGET_LEADS,
             include_county: bool = True):
    cancel_event = _CANCEL_EVENTS[job_id]
    listings: List[Dict] = []
    final_rows: List[Optional[Dict]] = []
    owners_found = 0
    phones_found = 0
    emails_found = 0
    websites_found = 0
    try:
        _update(job_id, status="running")
        _log(job_id, f"Looking up '{place}'...")

        resolved = geocode(place)
        lat = resolved["lat"]
        lon = resolved["lon"]
        display_name = resolved["display_name"]
        bbox = resolved.get("boundingbox")
        parent_query = resolved.get("parent_query")
        _log(job_id, f"Found: {display_name}")
        if parent_query and include_county:
            _log(job_id, f"  Surrounding area available: {parent_query}")

        # Discovery is Google Maps only (Apify). OSM/Overpass is not used.
        _log(job_id, "1/3 Google Maps wider discovery via Apify "
                     "(phrases + area + satellite"
                     + (" + county" if include_county else "")
                     + "; skipping closed places)...")

        try:
            rows = _discover_maps_leads(
                job_id,
                place,
                display_name,
                lat,
                lon,
                radius_km,
                categories,
                name_terms,
                target_leads,
                cancel_event,
                bbox=bbox,
                parent_query=parent_query,
                include_county=include_county,
            )
        except apify_maps.ApifyError as exc:
            _update(job_id, status="error", error=str(exc))
            _log(job_id, f"Error: {exc}")
            return

        rows = _apply_lead_quality_filters(job_id, rows)
        after_quality = len(rows)
        rows = _sort_and_trim(job_id, rows, target_leads)
        listings = rows
        _log(
            job_id,
            f"-> {len(rows)} independent-business listings to process "
            f"(after quality filters: {after_quality}).",
        )
        _update(job_id, total=len(rows))

        if not rows:
            final_status = "stopped" if cancel_event.is_set() else "error"
            _update(job_id, status=final_status)
            if final_status == "error":
                _update(job_id, error="No listings found. "
                                       "Try different business types or a bigger city nearby.")
            else:
                _log(job_id, "Stopped before anything was found.")
            return

        if len(rows) < target_leads and not cancel_event.is_set():
            _log(job_id, f"Note: only found {len(rows)} listings "
                         f"(target was {target_leads}).")

        with_sites = sum(1 for r in rows if r.get("website"))
        if enrich:
            _log(job_id, f"2/3 Website contact crawl on {len(rows)} listings"
                         f" ({with_sites} have websites; {ENRICHMENT_WORKERS} at a time)...")
            if not COMPANIES_HOUSE_API_KEY:
                _log(job_id, "  Companies House key not set — owner fallback / office check skipped.")
            else:
                _log(job_id, "3/3 Companies House check runs for each listing after the site crawl "
                             "(live companies only; website owner kept when found).")
        else:
            _log(job_id, f"Processing {len(rows)} listings (enrichment off)...")

        final_rows = [None] * len(rows)
        progress_lock = threading.Lock()
        completed = 0

        def process_one(row: Dict) -> Dict:
            if cancel_event.is_set():
                return _basic_listing_row(row, "stopped by user before this listing was processed")
            enrichment = {
                "owner_name": "",
                "owner_evidence": "",
                "owner_source": "none",
                "ch_registered_office": "",
                "company_number": "",
                "sic_code": "",
                "phone": row.get("phone", ""),
                "email": row.get("email", ""),
            }
            # Always try name extraction when enriching — owner/partner is mandatory.
            if enrich:
                enrichment = enrich_listing(row, cancel_event=cancel_event)
            trading = row.get("address", "") or ""
            ch_office = enrichment.get("ch_registered_office", "") or ""
            company_number = enrichment.get("company_number", "") or ""
            sic_code = enrichment.get("sic_code", "") or ""
            owner_evidence = enrichment.get("owner_evidence", "")
            owner_source = enrichment.get("owner_source", "none")
            mismatch = companies_house.addresses_mismatch(trading, ch_office)
            result = {
                "Business Name": row["business_name"],
                "Owner / Partner Name": enrichment.get("owner_name", ""),
                "Business Address": trading,
                "Phone": enrichment.get("phone") or row.get("phone", ""),
                "Email": enrichment.get("email") or row.get("email", ""),
                "Website": row.get("website", ""),
                "Category": row.get("category", ""),
                "Owner Evidence": owner_evidence,
                "Owner Source": owner_source,
                "Trading Address": trading,
                "CH Registered Office": ch_office,
                "Company Number": company_number,
                "Address Mismatch": "yes" if mismatch else "no",
                "owner_source": owner_source,
                "owner_evidence": owner_evidence,
                "trading_address": trading,
                "ch_registered_office": ch_office,
                "company_number": company_number,
                "sic_code": sic_code,
                "address_mismatch": mismatch,
                "lat": row.get("lat"),
                "lon": row.get("lon"),
            }
            return _attach_hours_fields(result, row)

        workers = ENRICHMENT_WORKERS if enrich else min(len(rows), 32)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(process_one, row): (i, row) for i, row in enumerate(rows)}
            for future in as_completed(futures):
                i, row = futures[future]

                if cancel_event.is_set():
                    for f in futures:
                        f.cancel()

                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    result = _basic_listing_row(row, f"error during lookup: {exc}")
                final_rows[i] = result
                _push_preview(job_id, result)

                with progress_lock:
                    completed += 1
                    if result["Owner / Partner Name"]:
                        owners_found += 1
                    if result["Phone"]:
                        phones_found += 1
                    if result["Email"]:
                        emails_found += 1
                    if result["Website"]:
                        websites_found += 1
                    progress_snapshot = completed
                    owners_snapshot = owners_found
                    phones_snapshot = phones_found
                    emails_snapshot = emails_found
                    sites_snapshot = websites_found

                _update(
                    job_id,
                    progress=progress_snapshot,
                    owners_found=owners_snapshot,
                    phones_found=phones_snapshot,
                    emails_found=emails_snapshot,
                    websites_found=sites_snapshot,
                )
                if completed % 5 == 0 or completed == len(rows):
                    _log(
                        job_id,
                        f"[{completed}/{len(rows)}] processed"
                        f" · {owners_snapshot} owners"
                        f" · {phones_snapshot} phones"
                        f" · {emails_snapshot} emails",
                    )

        for i, row in enumerate(rows):
            if final_rows[i] is None:
                final_rows[i] = _basic_listing_row(
                    row, "stopped by user before this listing was processed"
                )

        final_status = "stopped" if cancel_event.is_set() else "done"
        completed_rows = [r for r in final_rows if r]
        preview_rows = completed_rows[:PREVIEW_ROW_LIMIT]
        slot_counts = opening_hours.count_slots(completed_rows)
        # Persist done/stopped before writing the CSV so a --reload mid-write
        # can still recover a finished job from disk instead of 404.
        _update(
            job_id,
            status=final_status,
            result_count=len(completed_rows),
            owners_found=owners_found,
            phones_found=phones_found,
            emails_found=emails_found,
            websites_found=websites_found,
            preview=preview_rows,
            leads=completed_rows,
            dial_slot_counts=slot_counts,
        )

        csv_path = _write_job_csv(job_id, completed_rows)
        _update(job_id, csv_path=csv_path)
        verb = "Stopped early" if final_status == "stopped" else "Done"
        slot_bits = ", ".join(
            f"{opening_hours.DIAL_SLOTS[sid][0]}={slot_counts.get(sid, 0)}"
            for sid in opening_hours.DIAL_SLOTS
        )
        _log(
            job_id,
            f"{verb}. {len(completed_rows)} listings saved · "
            f"{owners_found} owners · {phones_found} phones · {emails_found} emails. "
            f"Dial windows: {slot_bits}.",
        )

    except Exception as exc:  # noqa: BLE001
        salvage = _salvage_csv_rows(
            listings,
            final_rows,
            f"job interrupted before this listing was processed: {exc}",
        )
        if salvage:
            try:
                csv_path = _write_job_csv(job_id, salvage)
                preview_rows = salvage[:PREVIEW_ROW_LIMIT]
                slot_counts = opening_hours.count_slots(salvage)
                _update(
                    job_id,
                    status="error",
                    error=str(exc),
                    csv_path=csv_path,
                    result_count=len(salvage),
                    owners_found=owners_found,
                    phones_found=phones_found,
                    emails_found=emails_found,
                    websites_found=websites_found,
                    preview=preview_rows,
                    leads=salvage,
                    dial_slot_counts=slot_counts,
                )
                _log(
                    job_id,
                    f"Error: {exc}. Saved partial CSV with {len(salvage)} listing(s) — download available.",
                )
            except Exception as write_exc:  # noqa: BLE001
                _update(job_id, status="error", error=str(exc))
                _log(job_id, f"Error: {exc}")
                _log(job_id, f"Could not save partial CSV: {write_exc}")
        else:
            _update(job_id, status="error", error=str(exc))
            _log(job_id, f"Error: {exc}")
    finally:
        _CANCEL_EVENTS.pop(job_id, None)
