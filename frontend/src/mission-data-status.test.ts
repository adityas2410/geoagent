import { describe, expect, it } from "vitest";
import { missionDataStatus } from "./mission-data-status";
import type { MissionEvent, MissionMapState } from "./types";

const baseState: MissionMapState = {
  revision: 1,
  updated_at: "2026-08-29T00:00:00Z",
  is_final: false,
  availability: {
    locations: "not_requested",
    routes: "not_requested",
    assignments: "not_requested",
    metrics: "not_requested",
    validation: "not_requested",
  },
  locations: [],
  routes: [],
  assignments: [],
  warnings: [],
};

function event(type: MissionEvent["type"], tool: string): MissionEvent {
  return {
    event_id: `${type}-${tool}`,
    mission_id: "mission-test",
    type,
    tool,
    payload: {},
    created_at: "2026-08-29T00:00:00Z",
  };
}

describe("Mission data status", () => {
  it("reports a pending tool result only while the Mission is running", () => {
    expect(missionDataStatus("routes", baseState, "running", [event("tool_called", "compute_routes")]))
      .toBe("Waiting for route result.");
  });

  it("distinguishes absent, returned-without-projection, and failed data", () => {
    expect(missionDataStatus("locations", baseState, "completed", [])).toBe("Not recorded for this Mission.");
    expect(missionDataStatus("locations", baseState, "completed", [event("tool_result", "geocode_locations")]))
      .toBe("No usable locations data was returned.");
    expect(missionDataStatus("locations", {
      ...baseState,
      availability: { ...baseState.availability, locations: "unavailable" },
    }, "completed", [])).toBe("Unavailable — see Agents for the tool result.");
  });

  it("explains an intentional backend skip with its persisted reason", () => {
    expect(missionDataStatus("routes", {
      ...baseState,
      availability: { ...baseState.availability, routes: "not_applicable" },
      availability_reasons: { routes: "The discovered work has no travel between locations." },
    }, "completed", [])).toBe("Not applicable — The discovered work has no travel between locations.");
  });
});
