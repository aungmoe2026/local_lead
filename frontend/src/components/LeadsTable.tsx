import { useEffect, useMemo, useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Checkbox } from "@/components/ui/checkbox"
import {
  AddressChip,
  LeadDetailPanel,
  SourceChip,
  type PreviewRow,
} from "@/components/LeadDetailPanel"

export const DIAL_SLOTS = [
  { id: "am1", label: "09:00–11:30" },
  { id: "am2", label: "11:30–13:00" },
  { id: "pm1", label: "13:00–15:00" },
  { id: "pm2", label: "15:00–17:00" },
] as const

const PAGE_SIZE = 25

type DialFilter = "all" | "unknown" | (typeof DIAL_SLOTS)[number]["id"]

function leadMatchesDial(row: PreviewRow, slotId: string): boolean {
  const slots = row.dial_slots
  if (Array.isArray(slots) && slots.length > 0) return slots[0] === slotId
  return false
}

function hasUnknownHours(row: PreviewRow): boolean {
  if (Array.isArray(row.dial_slots) && row.dial_slots.length > 0) return false
  const display = (row.opening_hours_display || row["Opening Hours"] || "").trim()
  const hours = row.opening_hours
  if (hours && typeof hours === "object" && Object.keys(hours).length > 0) return false
  return !display
}

export function LeadsTable({ leads }: { leads: PreviewRow[] }) {
  const [selected, setSelected] = useState(0)
  const [search, setSearch] = useState("")
  const [category, setCategory] = useState("")
  const [dial, setDial] = useState<DialFilter>("all")
  const [hasPhone, setHasPhone] = useState(false)
  const [hasWebsite, setHasWebsite] = useState(false)
  const [hasOwner, setHasOwner] = useState(false)
  const [page, setPage] = useState(0)

  const categories = useMemo(() => {
    const set = new Set<string>()
    for (const row of leads) {
      const c = (row.Category || "").trim()
      if (c) set.add(c)
    }
    return Array.from(set).sort((a, b) => a.localeCompare(b))
  }, [leads])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return leads.filter((row) => {
      if (category && (row.Category || "") !== category) return false
      if (hasPhone && !(row.Phone || "").trim()) return false
      if (hasWebsite && !(row.Website || "").trim()) return false
      if (hasOwner && !(row["Owner / Partner Name"] || "").trim()) return false
      if (dial === "unknown") {
        if (!hasUnknownHours(row)) return false
      } else if (dial !== "all") {
        if (!leadMatchesDial(row, dial)) return false
      }
      if (!q) return true
      const blob = [
        row["Business Name"],
        row["Owner / Partner Name"],
        row["Business Address"],
        row.trading_address,
        row.Phone,
        row.Email,
        row.Website,
        row.Category,
        row.company_number || row["Company Number"],
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
      return blob.includes(q)
    })
  }, [leads, search, category, dial, hasPhone, hasWebsite, hasOwner])

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const safePage = Math.min(page, pageCount - 1)
  const pageRows = filtered.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE)

  useEffect(() => {
    setPage(0)
  }, [search, category, dial, hasPhone, hasWebsite, hasOwner, leads.length])

  useEffect(() => {
    if (!pageRows.length) {
      setSelected(0)
      return
    }
    if (selected >= pageRows.length) setSelected(0)
  }, [pageRows, selected])

  const selectedRow = pageRows[selected] ?? null

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1 min-w-[180px] flex-1">
          <Label htmlFor="lead-search" className="text-xs">
            Search
          </Label>
          <Input
            id="lead-search"
            placeholder="Name, address, phone…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="h-8 text-sm"
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="lead-category" className="text-xs">
            Category
          </Label>
          <select
            id="lead-category"
            className="h-8 rounded-md border border-slate-200 bg-white px-2 text-sm text-slate-800"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
          >
            <option value="">All</option>
            {categories.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="lead-dial" className="text-xs">
            Dial window
          </Label>
          <select
            id="lead-dial"
            className="h-8 rounded-md border border-slate-200 bg-white px-2 text-sm text-slate-800"
            value={dial}
            onChange={(e) => setDial(e.target.value as DialFilter)}
          >
            <option value="all">All</option>
            {DIAL_SLOTS.map((s) => (
              <option key={s.id} value={s.id}>
                {s.label}
              </option>
            ))}
            <option value="unknown">Unknown hours</option>
          </select>
        </div>
        <label className="flex items-center gap-1.5 text-xs text-slate-700 pb-1.5">
          <Checkbox checked={hasPhone} onCheckedChange={() => setHasPhone((v) => !v)} />
          Phone
        </label>
        <label className="flex items-center gap-1.5 text-xs text-slate-700 pb-1.5">
          <Checkbox checked={hasWebsite} onCheckedChange={() => setHasWebsite((v) => !v)} />
          Website
        </label>
        <label className="flex items-center gap-1.5 text-xs text-slate-700 pb-1.5">
          <Checkbox checked={hasOwner} onCheckedChange={() => setHasOwner((v) => !v)} />
          Owner
        </label>
      </div>

      <div className="text-xs text-slate-500">
        Showing {filtered.length === 0 ? 0 : safePage * PAGE_SIZE + 1}–
        {Math.min((safePage + 1) * PAGE_SIZE, filtered.length)} of {filtered.length}
        {filtered.length !== leads.length ? ` (filtered from ${leads.length})` : ""}
      </div>

      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
        <div className="overflow-auto rounded-md border border-slate-200 max-h-[calc(100vh-6rem)]">
          <table className="w-full text-xs text-left">
            <thead className="bg-slate-100 text-slate-600 sticky top-0 z-10">
              <tr>
                <th className="px-2 py-1.5 font-medium">Business</th>
                <th className="px-2 py-1.5 font-medium">Owner</th>
                <th className="px-2 py-1.5 font-medium">Source</th>
                <th className="px-2 py-1.5 font-medium">Hours</th>
                <th className="px-2 py-1.5 font-medium bg-emerald-50/80 text-emerald-900">
                  Address
                </th>
              </tr>
            </thead>
            <tbody>
              {pageRows.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-2 py-6 text-center text-slate-400">
                    No leads match these filters.
                  </td>
                </tr>
              ) : (
                pageRows.map((row, i) => (
                  <tr
                    key={`${row["Business Name"]}-${i}-${safePage}`}
                    onClick={() => setSelected(i)}
                    className={`border-t border-slate-100 cursor-pointer ${
                      selected === i ? "bg-slate-100" : "hover:bg-slate-50"
                    }`}
                  >
                    <td className="px-2 py-1.5 text-slate-800 max-w-[160px] truncate">
                      {row["Business Name"]}
                    </td>
                    <td className="px-2 py-1.5 text-slate-600 max-w-[120px] truncate">
                      {row["Owner / Partner Name"] || "—"}
                    </td>
                    <td className="px-2 py-1.5">
                      <SourceChip source={row.owner_source} />
                    </td>
                    <td className="px-2 py-1.5 text-slate-500 max-w-[100px] truncate">
                      {row.opening_hours_display || row["Opening Hours"] || "—"}
                    </td>
                    <td className="px-2 py-1.5">
                      <AddressChip
                        trading={row.trading_address || row["Business Address"]}
                        mismatch={Boolean(row.address_mismatch)}
                      />
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
        <LeadDetailPanel row={selectedRow} />
      </div>

      {pageCount > 1 && (
        <div className="flex items-center justify-between gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={safePage <= 0}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
          >
            Previous
          </Button>
          <span className="text-xs text-slate-600 tabular-nums">
            Page {safePage + 1} of {pageCount}
          </span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={safePage >= pageCount - 1}
            onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
          >
            Next
          </Button>
        </div>
      )}
    </div>
  )
}

export function formatRemainingEta(seconds: number): string {
  if (seconds <= 15) return "Finishing…"
  if (seconds < 60) return `About ${Math.max(1, Math.round(seconds))} sec left`
  const mins = Math.round(seconds / 60)
  if (mins === 1) return "About 1 min left"
  if (mins < 60) return `About ${mins} min left`
  const hours = Math.floor(mins / 60)
  const rem = mins % 60
  if (rem === 0) return `About ${hours} hr left`
  return `About ${hours} hr ${rem} min left`
}

export function computeRemainingSeconds(opts: {
  now: number
  createdAt?: number
  etaLow?: number
  etaHigh?: number
  progress: number
  total: number
}): number | null {
  const { now, createdAt, etaLow, etaHigh, progress, total } = opts
  if (!createdAt) return null
  const elapsed = Math.max(0, now - createdAt)
  const mid =
    typeof etaLow === "number" && typeof etaHigh === "number"
      ? (etaLow + etaHigh) / 2
      : typeof etaHigh === "number"
        ? etaHigh
        : null

  if (progress > 0 && total > 0) {
    const rate = progress / Math.max(elapsed, 1)
    if (rate > 0) {
      const remaining = (total - progress) / rate
      return Math.max(0, remaining)
    }
  }
  if (mid == null) return null
  return Math.max(0, mid - elapsed)
}
