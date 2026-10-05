import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Progress } from "@/components/ui/progress";
import { Slider } from "@/components/ui/slider";
import { LocationMap } from "@/components/LocationMap";
import { type PreviewRow } from "@/components/LeadDetailPanel";
import {
  LeadsTable,
  DIAL_SLOTS,
  computeRemainingSeconds,
  formatRemainingEta,
} from "@/components/LeadsTable";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  MapPin,
  Search,
  Download,
  Loader2,
  AlertCircle,
  CheckCircle2,
  Square,
  Phone,
  Mail,
  User,
  Globe,
} from "lucide-react";

const CATEGORY_OPTIONS: { key: string; label: string }[] = [
  { key: "garage", label: "Garages" },
  { key: "fabric", label: "Fabric shops" },
  { key: "fish_chips", label: "Fish and chips" },
  { key: "florist", label: "Florists" },
  { key: "tattoo", label: "Tattoo shops" },
  { key: "jewellery", label: "Jewellery" },
  { key: "butcher", label: "Butchers" },
  { key: "tanning", label: "Tanning shops" },
  { key: "wool_craft", label: "Wool and craft" },
];

type JobStatus =
  | "idle"
  | "queued"
  | "running"
  | "stopping"
  | "stopped"
  | "done"
  | "error";

interface JobState {
  status: JobStatus;
  progress: number;
  total: number;
  logs: string[];
  error: string | null;
  result_count: number;
  owners_found: number;
  phones_found?: number;
  emails_found?: number;
  websites_found?: number;
  preview?: PreviewRow[];
  leads?: PreviewRow[];
  dial_slot_counts?: Record<string, number>;
  eta_label?: string;
  eta_seconds_low?: number;
  eta_seconds_high?: number;
  created_at?: number;
}

export default function App() {
  const [place, setPlace] = useState("");
  const [radius, setRadius] = useState("12");
  const [targetLeads, setTargetLeads] = useState("100");
  const [categories, setCategories] = useState<string[]>([]);
  const [nameSearch, setNameSearch] = useState("");
  const [enrich, setEnrich] = useState(true);
  const [includeCounty, setIncludeCounty] = useState(true);
  const [googleMapsAvailable, setGoogleMapsAvailable] = useState(false);
  const [etaLabel, setEtaLabel] = useState<string | null>(null);
  const [nowTick, setNowTick] = useState(() => Date.now() / 1000);

  useEffect(() => {
    fetch("/api/data-sources")
      .then((res) => res.json())
      .then((data) =>
        setGoogleMapsAvailable(Boolean(data.google_maps_available)),
      )
      .catch(() => setGoogleMapsAvailable(false));
  }, []);

  const categoriesKey = categories.join(",");

  useEffect(() => {
    const nameTerms = nameSearch
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    if (!categoriesKey && nameTerms.length === 0) {
      setEtaLabel(null);
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      fetch("/api/estimate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          categories: categoriesKey ? categoriesKey.split(",") : [],
          name_search: nameTerms,
          enrich,
          target_leads: parseInt(targetLeads, 10) || 100,
          include_county: includeCounty,
        }),
      })
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          if (data?.eta_label) setEtaLabel(data.eta_label);
        })
        .catch(() => {
          /* ignore abort / offline */
        });
    }, 250);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [categoriesKey, nameSearch, enrich, targetLeads, includeCounty]);

  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<JobState | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const pollRef = useRef<number | null>(null);
  const logsEndRef = useRef<HTMLDivElement | null>(null);

  const toggleCategory = (key: string) => {
    setCategories((prev) =>
      prev.includes(key) ? prev.filter((c) => c !== key) : [...prev, key],
    );
  };

  const selectAllCategories = () => {
    setCategories(CATEGORY_OPTIONS.map((c) => c.key));
  };

  const startJob = async () => {
    setFormError(null);

    if (!place.trim()) {
      setFormError("Enter a town, city, or postcode to search near.");
      return;
    }
    if (categories.length === 0 && !nameSearch.trim()) {
      setFormError(
        "Pick at least one business type, or add a name search term.",
      );
      return;
    }
    if (!googleMapsAvailable) {
      setFormError(
        "Add APIFY_API_TOKEN to backend/.env — Google Maps is required for discovery.",
      );
      return;
    }

    const body = {
      place: place.trim(),
      radius_km: parseFloat(radius) || 12,
      categories,
      name_search: nameSearch
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      enrich,
      target_leads: parseInt(targetLeads, 10) || 100,
      include_county: includeCounty,
    };

    try {
      const res = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json();
        setFormError(
          err.detail?.[0]?.msg ||
            err.detail ||
            "Something went wrong starting the search.",
        );
        return;
      }
      const data = await res.json();
      setJobId(data.job_id);
      setStopping(false);
      setJob({
        status: "queued",
        progress: 0,
        total: 0,
        logs: [],
        error: null,
        result_count: 0,
        owners_found: 0,
        phones_found: 0,
        emails_found: 0,
        websites_found: 0,
        preview: [],
        leads: [],
        dial_slot_counts: {},
        eta_label: data.eta_label,
        eta_seconds_low: data.eta_seconds_low,
        eta_seconds_high: data.eta_seconds_high,
        created_at: Date.now() / 1000,
      });
    } catch {
      setFormError(
        "Couldn't reach the local server. Make sure the app is running.",
      );
    }
  };

  const stopJob = async () => {
    if (!jobId) return;
    setStopping(true);
    try {
      await fetch(`/api/jobs/${jobId}/stop`, { method: "POST" });
    } catch {
      // the next poll will reflect whatever the real state turns out to be
    }
  };

  useEffect(() => {
    if (!jobId) return;

    let consecutiveFailures = 0;

    const failJob = (message: string) => {
      if (pollRef.current) window.clearInterval(pollRef.current);
      setJob((prev) => ({
        status: "error",
        progress: prev?.progress ?? 0,
        total: prev?.total ?? 0,
        logs: prev?.logs ?? [],
        error: message,
        result_count: prev?.result_count ?? 0,
        owners_found: prev?.owners_found ?? 0,
        phones_found: prev?.phones_found ?? 0,
        emails_found: prev?.emails_found ?? 0,
        websites_found: prev?.websites_found ?? 0,
        preview: prev?.preview ?? [],
        leads: prev?.leads ?? [],
        dial_slot_counts: prev?.dial_slot_counts,
      }));
    };

    const poll = async () => {
      try {
        const res = await fetch(`/api/jobs/${jobId}`);
        if (res.status === 404) {
          failJob(
            "Job lost (backend restarted or cleared). Start a new search.",
          );
          return;
        }
        if (!res.ok) {
          consecutiveFailures += 1;
          if (consecutiveFailures >= 3) {
            failJob(
              `Backend error (HTTP ${res.status}). Check the uvicorn terminal.`,
            );
          }
          return;
        }
        consecutiveFailures = 0;
        const data = await res.json();
        setJob(data);
        if (
          data.status === "done" ||
          data.status === "error" ||
          data.status === "stopped"
        ) {
          if (pollRef.current) window.clearInterval(pollRef.current);
        }
      } catch {
        consecutiveFailures += 1;
        if (consecutiveFailures >= 3) {
          failJob("Cannot reach backend on port 8000. Is uvicorn running?");
        }
      }
    };

    poll();
    pollRef.current = window.setInterval(poll, 800);
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [jobId]);

  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [job?.logs.length]);

  const isRunning =
    job?.status === "queued" ||
    job?.status === "running" ||
    job?.status === "stopping";

  useEffect(() => {
    if (!isRunning) return;
    setNowTick(Date.now() / 1000);
    const id = window.setInterval(() => setNowTick(Date.now() / 1000), 1000);
    return () => window.clearInterval(id);
  }, [isRunning, jobId]);

  const remainingEtaLabel = useMemo(() => {
    if (!job || !isRunning) return null;
    const secs = computeRemainingSeconds({
      now: nowTick,
      createdAt: job.created_at,
      etaLow: job.eta_seconds_low,
      etaHigh: job.eta_seconds_high,
      progress: job.progress,
      total: job.total,
    });
    if (secs == null) return null;
    return formatRemainingEta(secs);
  }, [job, isRunning, nowTick]);

  const tableLeads = job?.leads?.length ? job.leads : (job?.preview ?? []);
  const isFinished =
    job?.status === "done" ||
    job?.status === "stopped" ||
    job?.status === "error";
  const percent =
    job && job.total > 0 ? Math.round((job.progress / job.total) * 100) : 0;
  const canDownload =
    Boolean(jobId) &&
    (job?.result_count ?? 0) > 0 &&
    (job?.status === "done" ||
      job?.status === "stopped" ||
      job?.status === "error");
  const downloadPartial = job?.status === "error" || job?.status === "stopped";

  const slotCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const s of DIAL_SLOTS) counts[s.id] = 0;
    for (const row of tableLeads) {
      // Exclusive: one lead → one window (earliest slot stored / first in list)
      const id = row.dial_slots?.[0];
      if (id && id in counts) counts[id] += 1;
    }
    // Prefer recomputing from hours when dial_slots still has legacy multi-membership
    // but opening_hours is present — backend download already exclusivizes via hours.
    if (
      Object.values(counts).reduce((a, b) => a + b, 0) === 0 &&
      job?.dial_slot_counts
    ) {
      return job.dial_slot_counts;
    }
    return counts;
  }, [job?.dial_slot_counts, tableLeads]);

  const reset = () => {
    setJobId(null);
    setJob(null);
    setStopping(false);
  };

  return (
    <div className="min-h-screen bg-slate-50 py-10 px-4">
      <div className="mx-auto max-w-6xl space-y-6">
        <div className="text-center space-y-1">
          <h1 className="text-2xl font-bold text-slate-900">
            Local Business Leads By Tel
          </h1>
          <p className="text-sm text-slate-500">
            One click → list of local businesses with phone, email & owner name
            where available. No Google hunting. Download a CSV when it finishes.
          </p>
        </div>

        <Card>
          <CardHeader>
            <CardTitle>Search</CardTitle>
            <CardDescription>
              Type a place, keep the defaults, hit Generate. Search widens
              automatically until enough leads show up.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="space-y-2">
              <Label htmlFor="place">Town, city, or postcode</Label>
              <div className="relative">
                <MapPin className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-400" />
                <Input
                  id="place"
                  placeholder="e.g. Manchester, UK"
                  className="pl-9"
                  value={place}
                  onChange={(e) => setPlace(e.target.value)}
                  disabled={isRunning}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !isRunning) startJob();
                  }}
                />
              </div>
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label htmlFor="radius">Search radius</Label>
                <span className="text-sm font-medium text-slate-700 tabular-nums">
                  {radius || 0} km
                </span>
              </div>
              <div className="flex items-center gap-3">
                <Slider
                  id="radius"
                  min={1}
                  max={50}
                  step={1}
                  value={[Number(radius) || 12]}
                  onValueChange={([v]) => setRadius(String(v))}
                  disabled={isRunning}
                  className="flex-1"
                />
                <Input
                  type="number"
                  min={1}
                  max={50}
                  value={radius}
                  onChange={(e) => setRadius(e.target.value)}
                  disabled={isRunning}
                  className="w-20 shrink-0"
                  aria-label="Search radius in kilometres"
                />
              </div>
              <p className="text-xs text-slate-400">
                Maps search area around the place. If fewer leads than the
                target are found, the run widens this radius and then tries
                nearby satellite areas.
              </p>
              <label className="flex items-start gap-2 text-sm text-slate-700 pt-1">
                <Checkbox
                  checked={includeCounty}
                  onCheckedChange={() => setIncludeCounty((v) => !v)}
                  disabled={isRunning}
                  className="mt-0.5"
                />
                <span>
                  Include surrounding county / wider area
                  <span className="block text-xs text-slate-400 font-normal mt-0.5">
                    When still short of the target, runs an extra Maps pass on
                    the parent county or region.
                  </span>
                </span>
              </label>
            </div>

            <LocationMap place={place} radiusKm={Number(radius) || 12} />

            <div className="space-y-2">
              <Label htmlFor="target">Target lead count (best effort)</Label>
              <Input
                id="target"
                type="number"
                min={1}
                max={1000}
                value={targetLeads}
                onChange={(e) => setTargetLeads(e.target.value)}
                disabled={isRunning}
              />
              <p className="text-xs text-slate-400">
                Best effort — final count may be slightly under, or up to about
                30 over when Maps returns a dense batch. Closed places, chains,
                retail parks and head offices are skipped.
              </p>
            </div>

            <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700">
              <div className="font-medium">Discovery: Google Maps (wider)</div>
              <div className="text-xs text-slate-500 mt-0.5">
                {googleMapsAvailable
                  ? "Via Apify — related phrases, radius/county expand; skips closed, chains, retail parks and head offices; then website crawl and Companies House."
                  : "Needs APIFY_API_TOKEN in backend/.env before you can generate leads."}
              </div>
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label>Business types</Label>
                <button
                  type="button"
                  className="text-xs text-slate-500 underline-offset-2 hover:underline disabled:opacity-50"
                  onClick={selectAllCategories}
                  disabled={isRunning}
                >
                  Select all
                </button>
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                {CATEGORY_OPTIONS.map((opt) => (
                  <label
                    key={opt.key}
                    className="flex items-center gap-2 text-sm text-slate-700"
                  >
                    <Checkbox
                      checked={categories.includes(opt.key)}
                      onCheckedChange={() => toggleCategory(opt.key)}
                      disabled={isRunning}
                    />
                    {opt.label}
                  </label>
                ))}
              </div>
            </div>

            <div className="space-y-2">
              <Label htmlFor="name-search">
                Also match by name{" "}
                <span className="text-slate-400 font-normal">(optional)</span>
              </Label>
              <Input
                id="name-search"
                placeholder="e.g. pet shop, estate agent"
                value={nameSearch}
                onChange={(e) => setNameSearch(e.target.value)}
                disabled={isRunning}
              />
            </div>

            <label className="flex items-center gap-2 text-sm text-slate-700">
              <Checkbox
                checked={enrich}
                onCheckedChange={() => setEnrich((v) => !v)}
                disabled={isRunning}
              />
              Look up owner / phone / email from each website (recommended -
              still fast)
            </label>

            {formError && (
              <div className="flex items-start gap-2 text-sm text-red-600 bg-red-50 rounded-md p-3">
                <AlertCircle className="h-4 w-4 mt-0.5 shrink-0" />
                {formError}
              </div>
            )}
          </CardContent>
          <CardFooter className="flex-col gap-2 items-stretch">
            {etaLabel && !isRunning && (
              <p className="text-xs text-center text-slate-500">
                Estimated time to fetch:{" "}
                <span className="font-medium text-slate-700">{etaLabel}</span>
                <span className="text-slate-400">
                  {" "}
                  (rough — maps + website lookups vary)
                </span>
              </p>
            )}
            <Button
              className="w-full"
              size="lg"
              onClick={startJob}
              disabled={isRunning || !googleMapsAvailable}
            >
              {isRunning ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" /> Searching...
                </>
              ) : (
                <>
                  <Search className="h-4 w-4" /> Generate Leads
                </>
              )}
            </Button>
          </CardFooter>
        </Card>

        {job && (
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                {job.status === "done" && (
                  <CheckCircle2 className="h-5 w-5 text-green-600" />
                )}
                {job.status === "stopped" && (
                  <Square className="h-5 w-5 text-amber-600" />
                )}
                {job.status === "error" && (
                  <AlertCircle className="h-5 w-5 text-red-600" />
                )}
                {isRunning && (
                  <Loader2 className="h-5 w-5 animate-spin text-slate-500" />
                )}
                {job.status === "done"
                  ? "Done — downloads ready"
                  : job.status === "stopped"
                    ? "Stopped - partial results saved"
                    : job.status === "error"
                      ? "Something went wrong"
                      : job.status === "stopping"
                        ? "Stopping..."
                        : "Working..."}
              </CardTitle>
              {job.total > 0 && (
                <CardDescription>
                  {job.progress} of {job.total} businesses processed
                  {isRunning && remainingEtaLabel
                    ? ` · ${remainingEtaLabel}`
                    : ""}
                </CardDescription>
              )}
              {isRunning && job.total === 0 && (
                <CardDescription>
                  {remainingEtaLabel
                    ? `${remainingEtaLabel} (discovery in progress)`
                    : job.eta_label
                      ? `Est. total ${job.eta_label} (discovery in progress)`
                      : "Discovery in progress"}
                </CardDescription>
              )}
            </CardHeader>
            <CardContent className="space-y-4">
              {job.total > 0 && <Progress value={percent} />}

              {(job.owners_found > 0 ||
                (job.phones_found ?? 0) > 0 ||
                (job.emails_found ?? 0) > 0) && (
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
                  <Stat
                    icon={<User className="h-3.5 w-3.5" />}
                    label="Names"
                    value={job.owners_found}
                  />
                  <Stat
                    icon={<Phone className="h-3.5 w-3.5" />}
                    label="Phones"
                    value={job.phones_found ?? 0}
                  />
                  <Stat
                    icon={<Mail className="h-3.5 w-3.5" />}
                    label="Emails"
                    value={job.emails_found ?? 0}
                  />
                  <Stat
                    icon={<Globe className="h-3.5 w-3.5" />}
                    label="Websites"
                    value={job.websites_found ?? 0}
                  />
                </div>
              )}

              {job.error && (
                <div className="flex items-start gap-2 text-sm text-red-600 bg-red-50 rounded-md p-3">
                  <AlertCircle className="h-4 w-4 mt-0.5 shrink-0" />
                  {job.error}
                </div>
              )}

              {tableLeads.length > 0 && <LeadsTable leads={tableLeads} />}

              {job.logs.length > 0 && (
                <div className="h-36 overflow-y-auto rounded-md bg-slate-900 p-3 font-mono text-xs text-slate-100 space-y-0.5">
                  {job.logs.slice(-100).map((line, i) => (
                    <div key={i}>{line}</div>
                  ))}
                  <div ref={logsEndRef} />
                </div>
              )}
            </CardContent>
            <CardFooter className="gap-3 flex-wrap">
              {isRunning && jobId && (
                <Button variant="outline" onClick={stopJob} disabled={stopping}>
                  {stopping ? (
                    <>
                      <Loader2 className="h-4 w-4 animate-spin" /> Stopping...
                    </>
                  ) : (
                    <>
                      <Square className="h-4 w-4" /> Stop
                    </>
                  )}
                </Button>
              )}
              {canDownload && (
                <div className="flex flex-col gap-2 w-full">
                  <div className="text-xs text-slate-500">
                    Dial-window CSVs — each lead appears in only one slot
                    (earliest weekday window they are open). Leads with unknown
                    hours are only in All leads.
                    {downloadPartial ? " Partial run." : ""}
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 w-full">
                    {DIAL_SLOTS.map((slot) => {
                      const count = slotCounts[slot.id] ?? 0;
                      if (count === 0) {
                        return (
                          <Button
                            key={slot.id}
                            size="lg"
                            disabled
                            className="min-w-0"
                          >
                            <Download className="h-4 w-4 shrink-0" />
                            {slot.label} (0)
                          </Button>
                        );
                      }
                      return (
                        <Button
                          key={slot.id}
                          asChild
                          size="lg"
                          className="min-w-0"
                        >
                          <a
                            href={`/api/jobs/${jobId}/download?slot=${slot.id}`}
                            download
                          >
                            <Download className="h-4 w-4 shrink-0" />
                            {slot.label} ({count})
                          </a>
                        </Button>
                      );
                    })}
                  </div>
                  <Button
                    asChild
                    variant="outline"
                    size="sm"
                    className="self-start"
                  >
                    <a href={`/api/jobs/${jobId}/download`} download>
                      <Download className="h-4 w-4" />
                      All leads ({job.result_count})
                    </a>
                  </Button>
                </div>
              )}
              {isFinished && (
                <Button variant="outline" onClick={reset}>
                  New search
                </Button>
              )}
            </CardFooter>
          </Card>
        )}
      </div>
    </div>
  );
}

function Stat({
  icon,
  label,
  value,
}: {
  icon: ReactNode;
  label: string;
  value: number;
}) {
  return (
    <div className="flex items-center gap-2 rounded-md bg-slate-100 px-2.5 py-2 text-slate-700">
      {icon}
      <span>
        <span className="font-semibold">{value}</span> {label}
      </span>
    </div>
  );
}
