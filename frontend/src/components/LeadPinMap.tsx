import { MapContainer, TileLayer, Marker } from "react-leaflet"
import L from "leaflet"
import "leaflet/dist/leaflet.css"

const pinIcon = L.divIcon({
  className: "",
  html:
    '<div style="width:14px;height:14px;border-radius:9999px;' +
    'background:#0f172a;border:2px solid white;' +
    'box-shadow:0 0 0 1px rgba(15,23,42,0.35);"></div>',
  iconSize: [14, 14],
  iconAnchor: [7, 7],
})

export function LeadPinMap({ lat, lon }: { lat: number; lon: number }) {
  return (
    <div className="h-44 w-full overflow-hidden rounded-md border border-slate-200">
      <MapContainer
        key={`${lat},${lon}`}
        center={[lat, lon]}
        zoom={15}
        scrollWheelZoom={false}
        className="h-full w-full"
      >
        <TileLayer
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        />
        <Marker position={[lat, lon]} icon={pinIcon} />
      </MapContainer>
    </div>
  )
}
