"use client";

import { useEffect, useRef } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

import type { NavView } from "@/lib/events";

const STYLE: any = {
  version: 8,
  sources: { osm: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256,
    attribution: "© OpenStreetMap contributors" } },
  layers: [{ id: "osm", type: "raster", source: "osm", paint: { "raster-saturation": -0.7, "raster-contrast": -0.1 } }],
};

export default function CarMap({ nav }: { nav: NavView | null }) {
  const el = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const markers = useRef<maplibregl.Marker[]>([]);

  useEffect(() => {
    if (!el.current || map.current) return;
    map.current = new maplibregl.Map({ container: el.current, style: STYLE, center: [77.64, 12.99], zoom: 11 });
    map.current.on("load", () => {
      map.current!.addSource("route", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.current!.addLayer({ id: "route", type: "line", source: "route",
        paint: { "line-color": "#2f5bea", "line-width": 5 }, layout: { "line-cap": "round", "line-join": "round" } });
    });
    return () => { map.current?.remove(); map.current = null; };
  }, []);

  useEffect(() => {
    const m = map.current;
    if (!m) return;
    const coords = (nav?.polyline ?? []).map(([lat, lng]) => [lng, lat]);
    const apply = () => {
      markers.current.forEach((mk) => mk.remove());
      markers.current = [];
      if (coords.length < 2) {
        (m.getSource("route") as maplibregl.GeoJSONSource | undefined)?.setData({ type: "FeatureCollection", features: [] });
        return;
      }
      (m.getSource("route") as maplibregl.GeoJSONSource | undefined)?.setData({
        type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: coords },
      } as any);
      markers.current.forEach((mk) => mk.remove());
      markers.current = coords.map((c, i) => {
        const dot = document.createElement("div");
        const last = i === coords.length - 1;
        dot.style.cssText = `width:${i === 0 ? 12 : 14}px;height:${i === 0 ? 12 : 14}px;border-radius:50%;` +
          `background:${i === 0 ? "#1c2430" : last ? "#2f5bea" : "#b7791f"};border:2px solid white`;
        return new maplibregl.Marker({ element: dot }).setLngLat(c as [number, number]).addTo(m);
      });
      const b = coords.reduce((bb, c) => bb.extend(c as [number, number]),
        new maplibregl.LngLatBounds(coords[0] as [number, number], coords[0] as [number, number]));
      m.fitBounds(b, { padding: 60, duration: 600, maxZoom: 13 });
    };
    if (m.isStyleLoaded()) apply(); else m.once("load", apply);
    return () => { m.off("load", apply); };
  }, [nav]);

  return (
    <>
      <div ref={el} className="map" aria-label="Route map" />
      <div className="eta" aria-live="polite">
        {nav?.destination ? (
          <>
            <span><strong>{nav.eta} min</strong> to {nav.destination}</span>
            {nav.stops.length > 0 && <span>via {nav.stops.join(", ")}</span>}
          </>
        ) : (
          <span className="empty">Say where you want to go, for example “take me to the airport”.</span>
        )}
      </div>
    </>
  );
}
