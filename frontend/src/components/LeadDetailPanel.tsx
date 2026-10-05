import type { ReactNode } from "react"
import { Building2, Clock, ExternalLink, Globe, Mail, Phone, User } from "lucide-react"
import { LeadPinMap } from "@/components/LeadPinMap"

export interface PreviewRow {
  "Business Name": string
  "Owner / Partner Name": string
  "Business Address": string
  Phone: string
  Email: string
  Website: string
  Category: string
  "Owner Evidence"?: string
  "Company Number"?: string
  "Opening Hours"?: string
  owner_source?: string
  owner_evidence?: string
  trading_address?: string
  ch_registered_office?: string
  company_number?: string
  address_mismatch?: boolean
  lat?: number | null
  lon?: number | null
  opening_hours?: Record<string, string[][]>
  opening_hours_display?: string
  dial_slots?: string[]
}

export function companyNumberOf(row: PreviewRow): string {
  return (row.company_number || row["Company Number"] || "").trim()
}

export function companiesHouseUrl(companyNumber: string) {
  return `https://find-and-update.company-information.service.gov.uk/company/${encodeURIComponent(companyNumber)}`
}

export function mapsSearchUrl(row: PreviewRow) {
  const q = [row["Business Name"], row.trading_address || row["Business Address"]]
    .filter(Boolean)
    .join(" ")
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(q)}`
}

export function mapsEmbedUrl(row: PreviewRow) {
  const q = [row["Business Name"], row.trading_address || row["Business Address"]]
    .filter(Boolean)
    .join(" ")
  return `https://www.google.com/maps?q=${encodeURIComponent(q)}&output=embed`
}

function sourcePlainLabel(source?: string): string {
  const s = source ?? "none"
  switch (s) {
    case "website":
      return "Owner from website"
    case "osm":
      return "Owner from OpenStreetMap tag"
    case "companies_house_psc":
      return "Owner from Companies House PSC"
    case "companies_house_director":
      return "Owner from Companies House director"
    case "none":
      return "No owner found"
    default:
      return "No owner found"
  }
}

export function SourceChip({ source }: { source?: string }) {
  const label =
    source === "website"
      ? "website"
      : source === "osm"
        ? "OSM"
        : source === "companies_house_psc"
          ? "CH PSC"
          : source === "companies_house_director"
            ? "CH director"
            : "none"
  const tone =
    source === "none" || !source
      ? "bg-slate-100 text-slate-500"
      : source.startsWith("companies_house")
        ? "bg-amber-50 text-amber-800"
        : "bg-emerald-50 text-emerald-800"
  return (
    <span className={`inline-block rounded px-1.5 py-0.5 text-[10px] font-medium ${tone}`}>
      {label}
    </span>
  )
}

export function AddressChip({
  trading,
  mismatch,
}: {
  trading?: string
  mismatch: boolean
}) {
  // Always a colored status chip (never plain text).
  if (!trading) {
    return (
      <span className="inline-block rounded px-1.5 py-0.5 text-[10px] font-medium bg-red-50 text-red-700 ring-1 ring-red-100">
        No trading address
      </span>
    )
  }
  if (mismatch) {
    return (
      <span className="inline-block rounded px-1.5 py-0.5 text-[10px] font-medium bg-amber-50 text-amber-800 ring-1 ring-amber-100">
        CH office differs
      </span>
    )
  }
  return (
    <span className="inline-block rounded px-1.5 py-0.5 text-[10px] font-medium bg-emerald-50 text-emerald-800 ring-1 ring-emerald-100">
      OK
    </span>
  )
}

function ContactLine({
  icon,
  label,
  children,
}: {
  icon: ReactNode
  label: string
  children: ReactNode
}) {
  return (
    <div className="flex items-start gap-2 text-sm">
      <span className="mt-0.5 text-slate-400 shrink-0">{icon}</span>
      <div className="min-w-0">
        <div className="text-[10px] uppercase tracking-wide text-slate-400">{label}</div>
        <div className="text-slate-800 break-all">{children}</div>
      </div>
    </div>
  )
}

export function LeadDetailPanel({ row }: { row: PreviewRow | null }) {
  if (!row) {
    return (
      <div className="flex h-full min-h-[280px] items-center justify-center rounded-md border border-dashed border-slate-200 bg-slate-50 px-4 text-center text-sm text-slate-400">
        Select a lead from the table to inspect owner, contacts, and addresses.
      </div>
    )
  }

  const owner = row["Owner / Partner Name"]
  const evidence = row.owner_evidence || row["Owner Evidence"] || ""
  const trading = row.trading_address || row["Business Address"] || ""
  const chOffice = row.ch_registered_office || ""
  const companyNumber = companyNumberOf(row)
  const source = row.owner_source

  return (
    <div className="rounded-md border border-slate-200 bg-white p-4 space-y-4 text-sm lg:sticky lg:top-4 max-h-[calc(100vh-6rem)] overflow-y-auto">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 space-y-1">
          <h3 className="text-base font-semibold text-slate-900 leading-snug">
            {row["Business Name"]}
          </h3>
          {row.Category && (
            <span className="inline-block rounded bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-600">
              {row.Category}
            </span>
          )}
        </div>
        <a
          href={mapsSearchUrl(row)}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 text-xs text-slate-600 underline-offset-2 hover:underline shrink-0"
        >
          Open in Google Maps <ExternalLink className="h-3 w-3" />
        </a>
      </div>

      <div className="flex flex-wrap items-center gap-2 rounded-md bg-slate-50 px-2.5 py-2">
        <SourceChip source={source} />
        <AddressChip trading={trading} mismatch={Boolean(row.address_mismatch)} />
        <span className="text-xs text-slate-600">{sourcePlainLabel(source)}</span>
      </div>

      <div className="space-y-1.5">
        <div className="text-[10px] uppercase tracking-wide text-slate-400 flex items-center gap-1">
          <User className="h-3 w-3" /> Owner / partner
        </div>
        <div className="text-lg font-medium text-slate-900">
          {owner || "Not found"}
        </div>
        <p className="text-xs text-slate-500 leading-relaxed">
          {evidence || "No owner evidence recorded for this lead."}
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-1">
        <ContactLine icon={<Phone className="h-3.5 w-3.5" />} label="Phone">
          {row.Phone ? (
            <a href={`tel:${row.Phone}`} className="text-slate-800 hover:underline">
              {row.Phone}
            </a>
          ) : (
            <span className="text-slate-400">Not found</span>
          )}
        </ContactLine>
        <ContactLine icon={<Mail className="h-3.5 w-3.5" />} label="Email">
          {row.Email ? (
            <a href={`mailto:${row.Email}`} className="text-slate-800 hover:underline">
              {row.Email}
            </a>
          ) : (
            <span className="text-slate-400">Not found</span>
          )}
        </ContactLine>
        <ContactLine icon={<Globe className="h-3.5 w-3.5" />} label="Website">
          {row.Website ? (
            <a
              href={row.Website}
              target="_blank"
              rel="noreferrer"
              className="text-slate-800 hover:underline"
            >
              {row.Website}
            </a>
          ) : (
            <span className="text-slate-400">Not found</span>
          )}
        </ContactLine>
        <ContactLine icon={<Clock className="h-3.5 w-3.5" />} label="Opening hours">
          {row.opening_hours_display || row["Opening Hours"] ? (
            <span className="text-slate-800 whitespace-pre-wrap">
              {row.opening_hours_display || row["Opening Hours"]}
            </span>
          ) : (
            <span className="text-slate-400">Not available</span>
          )}
        </ContactLine>
        <ContactLine icon={<Building2 className="h-3.5 w-3.5" />} label="Company number">
          {companyNumber ? (
            <a
              href={companiesHouseUrl(companyNumber)}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 font-mono text-slate-800 hover:underline"
            >
              {companyNumber}
              <ExternalLink className="h-3 w-3 text-slate-400" />
            </a>
          ) : (
            <span className="text-slate-400">No CH match / not looked up</span>
          )}
        </ContactLine>
      </div>

      <div className="grid sm:grid-cols-2 gap-3 text-xs">
        <div className="rounded bg-slate-50 p-2.5 space-y-1">
          <div className="font-medium text-slate-700">Trading address</div>
          <div className="text-slate-600 leading-relaxed whitespace-pre-wrap">
            {trading || "Not found"}
          </div>
        </div>
        <div className="rounded bg-slate-50 p-2.5 space-y-1">
          <div className="font-medium text-slate-700">Companies House registered office</div>
          <div className="text-slate-600 leading-relaxed whitespace-pre-wrap">
            {chOffice || "No CH match / not looked up"}
          </div>
          {row.address_mismatch && (
            <div className="text-amber-700 font-medium pt-1">
              Postcodes differ — registered office is often the accountant, not the shop.
            </div>
          )}
        </div>
      </div>

      <div className="grid sm:grid-cols-2 gap-3">
        {typeof row.lat === "number" && typeof row.lon === "number" ? (
          <div className="space-y-1">
            <div className="text-xs font-medium text-slate-700">Trading pin (listing)</div>
            <LeadPinMap lat={row.lat} lon={row.lon} />
          </div>
        ) : (
          <div className="text-xs text-slate-400 flex items-center justify-center rounded-md border border-dashed border-slate-200 h-44">
            No lat/lon on this listing
          </div>
        )}
        <div className="space-y-1">
          <div className="text-xs font-medium text-slate-700">Google Maps (display only)</div>
          <iframe
            title="Google Maps preview"
            src={mapsEmbedUrl(row)}
            className="h-44 w-full rounded-md border border-slate-200"
            loading="lazy"
            referrerPolicy="no-referrer-when-downgrade"
          />
        </div>
      </div>
    </div>
  )
}
