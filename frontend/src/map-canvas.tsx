import { Loader } from "@googlemaps/js-api-loader";
import { useEffect, useRef, useState } from "react";
import type { MapAssignment, MapLocation, MapRoute } from "./types";

interface MapCanvasProps {
  locations: MapLocation[];
  routes?: MapRoute[];
  assignments?: MapAssignment[];
  highlightedResourceId?: string | null;
  emptyMessage?: string;
  onSelectLocation: (location: MapLocation | null) => void;
}

const browserMapsKey = import.meta.env.VITE_GOOGLE_MAPS_API_KEY as string | undefined;

let loader: Loader | undefined;

function resourceIdForRoute(route: MapRoute, assignments: MapAssignment[]): string | null {
  if (route.resource_id) return route.resource_id;
  if (!route.destination_location_id) return null;
  const matchingResources = new Set(
    assignments
      .filter((assignment) =>
        assignment.destination_location_id === route.destination_location_id
        && (!route.origin_location_id || assignment.origin_location_id === route.origin_location_id),
      )
      .map((assignment) => assignment.resource_id),
  );
  return matchingResources.size === 1 ? [...matchingResources][0] : null;
}

export function routesForSelectedResource(
  routes: MapRoute[],
  assignments: MapAssignment[],
  resourceId: string | null | undefined,
): MapRoute[] {
  if (!resourceId) return routes;
  return routes.filter((route) => resourceIdForRoute(route, assignments) === resourceId);
}

export function MapCanvas({
  locations,
  routes = [],
  assignments = [],
  highlightedResourceId,
  emptyMessage = "No real operational locations are available for this view yet.",
  onSelectLocation,
}: MapCanvasProps) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<any>(null);
  const overlays = useRef<any[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "unconfigured" | "error">(
    browserMapsKey ? "loading" : "unconfigured",
  );

  useEffect(() => {
    if (!browserMapsKey || !container.current) return;
    let cancelled = false;
    loader ||= new Loader({ apiKey: browserMapsKey, version: "weekly", libraries: ["geometry"] });
    void loader
      .importLibrary("maps")
      .then(() => {
        if (cancelled || !container.current) return;
        const maps = (window as any).google.maps;
        map.current = new maps.Map(container.current, {
          center: { lat: 20, lng: 0 },
          zoom: 2,
          disableDefaultUI: true,
          zoomControl: true,
          fullscreenControl: true,
          mapTypeControl: false,
          streetViewControl: false,
          colorScheme: maps.ColorScheme?.DARK,
          internalUsageAttributionIds: ["gmp_git_agentskills_v1"],
        });
        setState("ready");
      })
      .catch(() => {
        if (!cancelled) setState("error");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!map.current || state !== "ready") return;
    const maps = (window as any).google.maps;
    overlays.current.forEach((overlay) => overlay.setMap?.(null));
    overlays.current = [];

    const locationsById = new Map(locations.map((location) => [location.location_id, location]));
    const bounds = new maps.LatLngBounds();
    locations.forEach((location, index) => {
      const marker = new maps.Marker({
        position: { lat: location.latitude, lng: location.longitude },
        map: map.current,
        title: location.label,
        label: locations.length > 1 ? String(index + 1) : undefined,
      });
      marker.addListener("click", () => onSelectLocation(location));
      overlays.current.push(marker);
      bounds.extend(marker.getPosition());
    });

    const visibleRoutes = routesForSelectedResource(routes, assignments, highlightedResourceId);
    visibleRoutes.forEach((route) => {
      if (!route.encoded_polyline) return;
      const routeResourceId = resourceIdForRoute(route, assignments);
      const routeAssignment = assignments.find((assignment) => assignment.resource_id === routeResourceId);
      const highlighted = Boolean(highlightedResourceId && routeResourceId === highlightedResourceId);
      const path = maps.geometry?.encoding?.decodePath(route.encoded_polyline);
      if (!path) return;
      const polyline = new maps.Polyline({
        path,
        map: map.current,
        geodesic: true,
        strokeColor: highlighted ? "#59d9bd" : "#5d85f7",
        strokeOpacity: highlighted ? 1 : 0.74,
        strokeWeight: highlighted ? 6 : 4,
        zIndex: highlighted ? 3 : 1,
      });
      polyline.addListener("click", () => {
        const location = route.destination_location_id
          ? locationsById.get(route.destination_location_id)
          : routeAssignment?.destination_location_id
            ? locationsById.get(routeAssignment.destination_location_id)
            : undefined;
        onSelectLocation(location || null);
      });
      overlays.current.push(polyline);
    });

    if (!bounds.isEmpty()) map.current.fitBounds(bounds, 72);
  }, [assignments, highlightedResourceId, locations, onSelectLocation, routes, state]);

  return (
    <div className="map-shell">
      <div ref={container} className="map-canvas" aria-label="Operational map" />
      {state === "loading" && <div className="map-message">Loading Google Maps…</div>}
      {state === "unconfigured" && (
        <div className="map-message">
          Add a browser Maps key in <code>frontend/.env.local</code> to enable the interactive map.
        </div>
      )}
      {state === "error" && (
        <div className="map-message">Google Maps could not load. Check the browser key and referrer restrictions.</div>
      )}
      {state === "ready" && locations.length === 0 && (
        <div className="map-message">{emptyMessage}</div>
      )}
    </div>
  );
}
