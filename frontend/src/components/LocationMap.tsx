import { useEffect, useRef, useState } from "react"
import { MapContainer, TileLayer, Marker, Circle, useMap } from "react-leaflet"
import L from "leaflet"
import "leaflet/dist/leaflet.css"
import { MapPin, Loader2, AlertCircle } from "lucide-react"

// Custom lightweight marker so we don't need to wire up Leaflet's default
// PNG marker assets through the bundler (a well-known source of broken
// marker icons with Vite/webpack) — a small filled dot matches the app's
// flat, monochrome slate style better than the default red pin anyway.
const pinIcon = L.divIcon({
  className: "",
  html:
    '<div style="width:14px;height:14px;border-radius:9999px;' +
    'background:#0f172a;border:2px solid white;' +
    'box-shadow:0 0 0 1px rgba(15,23,42,0.35);"></div>',
  iconSize: [14, 14],
  iconAnchor: [7, 7],
})

interface Coords {
  lat: number
  lon: number
  displayName: string
}

/** Keeps the map framed around the circle whenever the place or radius changes. */
function FitToRadius({ lat, lon, radiusKm }: { lat: number; lon: number; radiusKm: number }) {
  const map = useMap()
  useEffect(() => {
    const bounds = L.latLng(lat, lon).toBounds(Math.max(radiusKm, 0.5) * 1000 * 2.3)
    map.fitBounds(bounds, { animate: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lat, lon, radiusKm])
  return null
}

export function LocationMap({ place, radiusKm }: { place: string; radiusKm: number }) {
  const [coords, setCoords] = useState<Coords | null>(null)
  const [status, setStatus] = useState<"idle" | "loading" | "error">("idle")
  const [errorMsg, setErrorMsg] = useState("")
  const abortRef = useRef<AbortController | null>(null)
  const debounceRef = useRef<number | null>(null)

  useEffect(() => {
    const trimmed = place.trim()
    if (debounceRef.current) window.clearTimeout(debounceRef.current)
    if (abortRef.current) abortRef.current.abort()

    if (!trimmed) {
      setCoords(null)
      setStatus("idle")
      return
    }

    debounceRef.current = window.setTimeout(() => {
      setStatus("loading")
      setErrorMsg("")
      const controller = new AbortController()
      abortRef.current = controller
      fetch(`/api/geocode?place=${encodeURIComponent(trimmed)}`, { signal: controller.signal })
        .then(async (res) => {
          if (!res.ok) {
            const err = await res.json().catch(() => null)
            throw new Error(err?.detail || "Couldn't find that place.")
          }
          return res.json()
        })
        .then((data) => {
          setCoords({ lat: data.lat, lon: data.lon, displayName: data.display_name })
          setStatus("idle")
        })
        .catch((e: Error) => {
          if (e.name === "AbortError") return
          setErrorMsg(e.message || "Couldn't reach the local server.")
          setStatus("error")
        })
    }, 700)

    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current)
    }
  }, [place])

  const showMap = coords !== null

  return (
    <div className="space-y-1.5">
      <div className="relative h-56 w-full overflow-hidden rounded-md border border-slate-200 bg-slate-50">
        {showMap ? (
          <>
            <MapContainer
              center={[coords!.lat, coords!.lon]}
              zoom={11}
              scrollWheelZoom={false}
              className="h-full w-full"
            >
              <TileLayer
                url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
                attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
              />
              <FitToRadius lat={coords!.lat} lon={coords!.lon} radiusKm={radiusKm} />
              <Marker position={[coords!.lat, coords!.lon]} icon={pinIcon} />
              <Circle
                center={[coords!.lat, coords!.lon]}
                radius={Math.max(radiusKm, 0.1) * 1000}
                pathOptions={{ color: "#0f172a", weight: 1.5, fillColor: "#0f172a", fillOpacity: 0.08 }}
              />
            </MapContainer>
            {status === "loading" && (
              <div className="absolute right-2 top-2 rounded-full bg-white/90 p-1.5 shadow">
                <Loader2 className="h-3.5 w-3.5 animate-spin text-slate-500" />
              </div>
            )}
          </>
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-1.5 px-4 text-center">
            {status === "loading" ? (
              <>
                <Loader2 className="h-5 w-5 animate-spin text-slate-400" />
                <p className="text-xs text-slate-400">Finding that place...</p>
              </>
            ) : status === "error" ? (
              <>
                <AlertCircle className="h-5 w-5 text-red-400" />
                <p className="text-xs text-red-500">{errorMsg}</p>
              </>
            ) : (
              <>
                <MapPin className="h-5 w-5 text-slate-300" />
                <p className="text-xs text-slate-400">Type a place above to preview the search area</p>
              </>
            )}
          </div>
        )}
      </div>
      {showMap && status === "error" && (
        <p className="text-xs text-red-500">{errorMsg} (showing last known location)</p>
      )}
      {showMap && status !== "error" && (
        <p className="truncate text-xs text-slate-400">{coords!.displayName}</p>
      )}
    </div>
  )
}
