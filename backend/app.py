"""
FastAPI app: serves the built frontend (static/) and exposes a small API
for the frontend to start a lead-gen job, poll its progress, and download
the finished CSV.

Run with:
    uvicorn app:app --host 127.0.0.1 --port 8000

Dev (auto-reload — exclude outputs so writing the CSV does not restart mid-job):
    uvicorn app:app --host 127.0.0.1 --port 8000 --reload ^
      --reload-exclude "outputs/*" --reload-exclude "__pycache__/*" --reload-exclude ".venv/*"

(the start_*.sh/.bat scripts in the project root do the non-reload form for you
and open the browser automatically)
"""

import os
import re
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import APIFY_API_TOKEN, CATEGORIES, DEFAULT_RADIUS_KM, DEFAULT_TARGET_LEADS
from geocode import GeocodeError, geocode
from estimate import estimate_job_seconds
import jobs
import opening_hours

BASE_DIR = os.path.dirname(__file__)
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="Local Leads")


class JobRequest(BaseModel):
    place: str = Field(..., min_length=1)
    radius_km: float = Field(DEFAULT_RADIUS_KM, gt=0, le=50)
    categories: list[str] = Field(default_factory=list)
    name_search: list[str] = Field(default_factory=list)
    enrich: bool = True
    target_leads: int = Field(DEFAULT_TARGET_LEADS, ge=1, le=1000)
    include_county: bool = True


@app.get("/api/health")
def health():
    return {"ok": True, "service": "local-leads"}


@app.get("/api/categories")
def get_categories():
    return {"categories": list(CATEGORIES.keys())}


@app.get("/api/data-sources")
def get_data_sources():
    """Google Maps via Apify is the only discovery source — needs APIFY_API_TOKEN."""
    return {"google_maps_available": bool(APIFY_API_TOKEN)}


class EstimateRequest(BaseModel):
    categories: list[str] = Field(default_factory=list)
    name_search: list[str] = Field(default_factory=list)
    enrich: bool = True
    target_leads: int = Field(DEFAULT_TARGET_LEADS, ge=1, le=1000)
    include_county: bool = True


@app.post("/api/estimate")
def estimate_duration(req: EstimateRequest):
    """Rough ETA for the current form settings (wider Maps discovery + enrich)."""
    unknown = [c for c in req.categories if c not in CATEGORIES]
    if unknown:
        raise HTTPException(400, f"Unknown categories: {unknown}")
    low, high, summary = estimate_job_seconds(
        categories=req.categories,
        name_terms=req.name_search,
        target_leads=req.target_leads,
        enrich=req.enrich,
        include_county=req.include_county,
    )
    return {
        "eta_seconds_low": low,
        "eta_seconds_high": high,
        "eta_label": summary,
    }


@app.get("/api/geocode")
def geocode_preview(place: str):
    """Resolve a place name to lat/lon for the frontend's radius-preview map,
    without starting a full lead-gen job."""
    place = place.strip()
    if not place:
        raise HTTPException(400, "Place is required")
    try:
        resolved = geocode(place)
    except GeocodeError as exc:
        raise HTTPException(404, str(exc))
    return {
        "lat": resolved["lat"],
        "lon": resolved["lon"],
        "display_name": resolved["display_name"],
        "boundingbox": resolved.get("boundingbox"),
        "parent_query": resolved.get("parent_query"),
    }


@app.post("/api/jobs")
def start_job(req: JobRequest):
    unknown = [c for c in req.categories if c not in CATEGORIES]
    if unknown:
        raise HTTPException(400, f"Unknown categories: {unknown}")
    if not req.categories and not req.name_search:
        raise HTTPException(400, "Choose at least one category or add a name search term.")
    if not APIFY_API_TOKEN:
        raise HTTPException(
            400,
            "Google Maps discovery needs APIFY_API_TOKEN set in backend/.env.",
        )

    job_id = jobs.create_job(
        place=req.place.strip(),
        radius_km=req.radius_km,
        categories=req.categories,
        name_terms=req.name_search,
        enrich=req.enrich,
        target_leads=req.target_leads,
        include_county=req.include_county,
    )
    low, high, summary = estimate_job_seconds(
        categories=req.categories,
        name_terms=req.name_search,
        target_leads=req.target_leads,
        enrich=req.enrich,
        include_county=req.include_county,
    )
    return {
        "job_id": job_id,
        "eta_seconds_low": low,
        "eta_seconds_high": high,
        "eta_label": summary,
    }


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    # don't leak the server filesystem path to the frontend
    safe_job = {k: v for k, v in job.items() if k != "csv_path"}
    safe_job["logs"] = job["logs"][-200:]  # cap payload size on long runs
    return safe_job


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] not in ("queued", "running"):
        raise HTTPException(400, f"Job is already {job['status']}")
    jobs.stop_job(job_id)
    return {"status": "stopping"}


@app.get("/api/jobs/{job_id}/download")
def download_csv(job_id: str, slot: Optional[str] = Query(None)):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] not in ("done", "stopped", "error") or not job.get("csv_path"):
        raise HTTPException(400, "No CSV available for this job yet")
    if not job.get("result_count"):
        raise HTTPException(400, "No CSV available for this job yet")
    place_slug = re_slug(job.get("place") or "leads")

    if slot:
        if slot not in opening_hours.DIAL_SLOTS:
            raise HTTPException(400, f"Unknown dial slot. Use one of: {list(opening_hours.DIAL_SLOTS)}")
        path = jobs.write_slot_csv(job_id, slot)
        if not path:
            raise HTTPException(400, f"No leads open during {opening_hours.DIAL_SLOTS[slot][0]}")
        label = opening_hours.DIAL_SLOTS[slot][0].replace("–", "-").replace(":", "")
        return FileResponse(
            path,
            media_type="text/csv",
            filename=f"leads_{place_slug}_{label}_{job_id}.csv",
        )

    path = jobs.write_all_csv(job_id) or job.get("csv_path")
    if not path:
        raise HTTPException(400, "No CSV available for this job yet")
    return FileResponse(
        path,
        media_type="text/csv",
        filename=f"leads_{place_slug}_{job_id}.csv",
    )


def re_slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")
    return (slug or "leads")[:40]


# Serve the built frontend. Must be mounted last so /api/* routes above take priority.
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
