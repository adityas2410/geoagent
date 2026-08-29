import type { Mission, MissionEvent, MissionMapState } from "./types";

export const dataTools = {
  locations: "geocode_locations",
  routes: "compute_routes",
  assignments: "optimize_assignments",
  metrics: "calculate_plan_metrics",
  validation: "validate_plan",
} as const;

export type DataCategory = keyof typeof dataTools;

export function missionDataStatus(
  category: DataCategory,
  state: MissionMapState | null,
  missionStatus: Mission["status"],
  events: MissionEvent[],
) {
  const availability = state?.availability[category];
  const tool = dataTools[category];
  const toolCalled = events.some((event) => event.type === "tool_called" && event.tool === tool);
  const toolReturned = events.some((event) => event.type === "tool_result" && event.tool === tool);
  const resultName = category === "metrics" ? "metrics" : `${category.slice(0, -1)} result`;

  if (availability === "available") return "Available";
  if (availability === "not_applicable") {
    const reason = state?.availability_reasons?.[category];
    return reason ? `Not applicable — ${reason}` : "Not applicable";
  }
  if (availability === "unavailable") return "Unavailable — see Agents for the tool result.";
  if (toolReturned) return `No usable ${category} data was returned.`;
  if (toolCalled) return missionStatus === "running" ? `Waiting for ${resultName}.` : "Tool result was not recorded.";
  return missionStatus === "running" ? "Not requested yet." : "Not recorded for this Mission.";
}
