import { describe, expect, it } from "vitest";
import {
  cumulativeMissionMetrics,
  describeEvent,
  hasEventDetails,
  isAgentEvent,
  isLifecycleEvent,
  mapAvailabilityMessage,
  missionRuntimeSeconds,
  presentationMessage,
  statusLabel,
} from "./display";
import type { MissionEvent, MissionRunMetrics } from "./types";

function run(id: string, startedAt: string, updatedAt: string, llmRequests: number): MissionRunMetrics {
  return {
    run_id: id,
    started_at: startedAt,
    updated_at: updatedAt,
    llm_requests: llmRequests,
    fallback_requests: 0,
    tool_calls: 2,
    specialist_delegations: 1,
    llm_requests_by_agent: { mission_manager: llmRequests },
    model_requests: { "gemini-test": llmRequests },
    model_failures: {},
    input_tokens: 10,
    output_tokens: 5,
    thinking_tokens: 0,
    cached_input_tokens: 0,
    tool_use_prompt_tokens: 0,
    total_tokens: 15,
  };
}

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

  it("uses the backend presentation message and ignores internal finding fields", () => {
    expect(presentationMessage({
      code: "DRIVER_BREAK_REQUIRED",
      constraint_id: "RULE-002",
      message: "A required break cannot fit within the available shift.",
    })).toBe("A required break cannot fit within the available shift.");
  });

  it("keeps Mission totals and elapsed runtime across replans", () => {
    const runs = [
      run("run-1", "2026-08-30T10:00:00Z", "2026-08-30T10:01:00Z", 3),
      run("run-2", "2026-08-30T11:00:00Z", "2026-08-30T11:02:30Z", 4),
    ];

    expect(cumulativeMissionMetrics(runs)?.llm_requests).toBe(7);
    expect(cumulativeMissionMetrics(runs)?.tool_calls).toBe(4);
    expect(cumulativeMissionMetrics(runs)?.llm_requests_by_agent).toEqual({ mission_manager: 7 });
    expect(missionRuntimeSeconds(runs, "completed")).toBe(210);
    expect(missionRuntimeSeconds(runs, "completed", true)).toBe(150);
  });
});
