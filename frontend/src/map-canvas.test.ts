import { describe, expect, it } from "vitest";
import { routesForSelectedResource } from "./map-canvas";
import type { MapAssignment, MapRoute } from "./types";

const routes: MapRoute[] = [
  { route_id: "route-a", resource_id: "VEH-001", waypoint_location_ids: [] },
  { route_id: "route-b", origin_location_id: "DEPOT", destination_location_id: "CUSTOMER-B", waypoint_location_ids: [] },
  { route_id: "route-c", waypoint_location_ids: [] },
];

const assignments: MapAssignment[] = [
  { task_id: "TASK-A", resource_id: "VEH-001", sequence: 1 },
  { task_id: "TASK-B", resource_id: "VEH-002", sequence: 1, origin_location_id: "DEPOT", destination_location_id: "CUSTOMER-B" },
];

describe("routesForSelectedResource", () => {
  it("returns every route until a resource is selected", () => {
    expect(routesForSelectedResource(routes, assignments, null)).toEqual(routes);
  });

  it("isolates explicit and assignment-derived routes for the selected resource", () => {
    expect(routesForSelectedResource(routes, assignments, "VEH-002").map((route) => route.route_id)).toEqual(["route-b"]);
    expect(routesForSelectedResource(routes, assignments, "VEH-001").map((route) => route.route_id)).toEqual(["route-a"]);
  });
});
