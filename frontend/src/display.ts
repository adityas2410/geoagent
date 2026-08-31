import type { MapAvailability, MissionEvent, MissionRunMetrics, MissionStatus } from "./types";

/** The Plan tab receives presentation-safe backend findings, never internal metadata. */
export function presentationMessage<T extends { message?: unknown }>(finding: T) {
  return typeof finding.message === "string" && finding.message.trim()
    ? finding.message
    : "A planning result needs review.";
}

function sumMetricMaps(values: Array<Record<string, number>>) {
  return values.reduce<Record<string, number>>((total, value) => {
    for (const [key, count] of Object.entries(value)) total[key] = (total[key] || 0) + count;
    return total;
  }, {});
}

export function cumulativeMissionMetrics(runs: MissionRunMetrics[]) {
  const latest = runs.at(-1);
  if (!latest) return undefined;
  const numeric = ["llm_requests", "fallback_requests", "tool_calls", "specialist_delegations", "input_tokens", "output_tokens", "thinking_tokens", "cached_input_tokens", "tool_use_prompt_tokens", "total_tokens"] as const;
  const total = { ...latest };
  for (const key of numeric) total[key] = runs.reduce((sum, run) => sum + run[key], 0);
  total.llm_requests_by_agent = sumMetricMaps(runs.map((run) => run.llm_requests_by_agent));
  total.model_requests = sumMetricMaps(runs.map((run) => run.model_requests));
  total.model_failures = sumMetricMaps(runs.map((run) => run.model_failures));
  total.started_at = runs[0].started_at;
  return total;
}

export function missionRuntimeSeconds(runs: MissionRunMetrics[], status: MissionStatus, currentOnly = false) {
  const selected = currentOnly ? runs.slice(-1) : runs;
  return selected.reduce((total, run, index) => {
    const start = Date.parse(run.started_at);
    const end = status === "running" && index === selected.length - 1 ? Date.now() : Date.parse(run.updated_at);
    return total + Math.max(0, (end - start) / 1000);
  }, 0);
}

const agentEventTypes = new Set([
  "task_delegated",
  "specialist_started",
  "specialist_completed",
  "tool_called",
  "tool_result",
  "model_requested",
  "model_completed",
  "model_failed",
]);

const lifecycleEventTypes = new Set([
  "mission_created",
  "mission_started",
  "clarification_requested",
  "clarification_answered",
  "objective_decision_requested",
  "objective_revision_accepted",
  "plan_published",
  "mission_completed",
  "mission_failed",
]);

export const statusLabel = (status: MissionStatus) =>
  ({
    created: "Created",
    running: "Running",
    awaiting_input: "Awaiting clarification",
    awaiting_objective_decision: "Awaiting objective decision",
    completed: "Completed",
    failed: "Failed",
  })[status];

export const humanize = (value?: string | null) =>
  (value || "—")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());

export const formatTime = (value?: string | null, withDate = false) => {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, {
    ...(withDate ? { month: "short", day: "numeric" } : {}),
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
};

export const formatDistance = (meters?: number | null) =>
  typeof meters === "number" ? `${(meters / 1000).toFixed(meters >= 100_000 ? 0 : 1)} km` : "—";

export const formatDuration = (seconds?: number | null) => {
  if (typeof seconds !== "number") return "—";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return hours ? `${hours}h ${minutes}m` : `${minutes} min`;
};

export const isAgentEvent = (event: MissionEvent) => agentEventTypes.has(event.type);

export const isLifecycleEvent = (event: MissionEvent) => lifecycleEventTypes.has(event.type);

export const hasEventDetails = (event: MissionEvent) => Object.keys(event.payload).length > 0;

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function summarizeResult(value: unknown): string {
  if (Array.isArray(value)) return `Returned ${value.length} result${value.length === 1 ? "" : "s"}`;
  const result = asRecord(value);
  if (!result) return value === undefined ? "Recorded a result" : "Returned a result";

  for (const key of ["locations", "routes", "assignments", "items", "results", "rows"]) {
    if (Array.isArray(result[key])) {
      const count = result[key].length;
      return `Returned ${count} ${humanize(key).toLowerCase()}`;
    }
  }
  if (typeof result.count === "number") return `Returned ${result.count} items`;
  if (typeof result.status === "string") return `Result status: ${humanize(result.status)}`;
  return "Returned a structured result";
}

export function mapAvailabilityMessage(
  availability: MapAvailability[keyof MapAvailability],
  missionStatus: MissionStatus,
): string {
  if (availability === "available") return "Available";
  if (availability === "unavailable") return "Unavailable — see Agents for the tool result.";
  return missionStatus === "running" ? "Not available yet" : "Not returned for this Mission";
}

export function describeEvent(event: MissionEvent): string {
  const result = event.payload.result;
  if (event.type === "tool_called") return `Calling ${humanize(event.tool)}`;
  if (event.type === "model_requested") return `Requesting ${String(event.payload.model || "Gemini")} response`;
  if (event.type === "model_completed") return `Received ${String(event.payload.model || "Gemini")} response`;
  if (event.type === "model_failed") return `${String(event.payload.model || "Gemini")} request failed`;
  if (event.type === "tool_result") return `${humanize(event.tool)}: ${summarizeResult(result)}`;
  if (event.type === "task_delegated") return `Delegated to ${humanize(String(event.payload.specialist || event.tool))}`;
  if (event.type === "specialist_started") return "Started delegated work";
  if (event.type === "specialist_completed") {
    return event.payload.status === "error"
      ? "Delegated work finished with an error"
      : `Delegated work completed: ${summarizeResult(result)}`;
  }
  if (event.type === "agent_message") return String(event.payload.text || "Reported an update");
  if (event.type === "plan_published") return "Published the operational plan";
  if (event.type === "mission_completed") return "Mission completed";
  if (event.type === "mission_started") return "Mission execution started";
  if (event.type === "mission_created") return "Mission created";
  if (event.type === "clarification_requested") return "Requested clarification";
  if (event.type === "objective_decision_requested") return "Requested an objective decision";
  if (event.type === "mission_failed") return "Mission failed";
  if (result && typeof result === "object") return "Recorded a structured result";
  return humanize(event.type);
}
