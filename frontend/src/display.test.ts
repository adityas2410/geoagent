import { describe, expect, it } from "vitest";
import {
  describeEvent,
  hasEventDetails,
  isAgentEvent,
  isLifecycleEvent,
  mapAvailabilityMessage,
  statusLabel,
} from "./display";
import type { MissionEvent } from "./types";

describe("frontend display helpers", () => {
  it("renders every backend Mission status without inventing a state", () => {
    expect(statusLabel("running")).toBe("Running");
    expect(statusLabel("awaiting_input")).toBe("Awaiting clarification");
    expect(statusLabel("awaiting_objective_decision")).toBe("Awaiting objective decision");
    expect(statusLabel("completed")).toBe("Completed");
  });

  it("describes only safe persisted event metadata", () => {
    const event: MissionEvent = {
      event_id: "evt_test",
      mission_id: "msn_test",
      type: "tool_result",
      agent: "geospatial_intelligence_agent",
      tool: "compute_routes",
      payload: { result: { status: "success" } },
      created_at: "2026-08-28T00:00:00Z",
    };
    expect(describeEvent(event)).toBe("Compute Routes: Result status: Success");
    expect(isAgentEvent(event)).toBe(true);
    expect(isLifecycleEvent(event)).toBe(false);
    expect(hasEventDetails(event)).toBe(true);
  });

  it("keeps agent activity separate from Mission lifecycle history", () => {
    const lifecycle: MissionEvent = {
      event_id: "evt_started",
      mission_id: "msn_test",
      type: "mission_started",
      payload: {},
      created_at: "2026-08-28T00:00:00Z",
    };
    expect(isAgentEvent(lifecycle)).toBe(false);
    expect(isLifecycleEvent(lifecycle)).toBe(true);
    expect(hasEventDetails(lifecycle)).toBe(false);
  });

  it("explains map availability without turning missing data into zeros", () => {
    expect(mapAvailabilityMessage("available", "running")).toBe("Available");
    expect(mapAvailabilityMessage("not_requested", "running")).toBe("Not available yet");
    expect(mapAvailabilityMessage("not_requested", "completed")).toBe("Not returned for this Mission");
    expect(mapAvailabilityMessage("unavailable", "failed")).toBe("Unavailable — see Agents for the tool result.");
  });
});
