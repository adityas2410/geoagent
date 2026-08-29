export type MissionStatus =
  | "created"
  | "running"
  | "awaiting_input"
  | "awaiting_objective_decision"
  | "completed"
  | "failed";

export interface Workspace {
  workspace_id: string;
  name: string;
  status: "active" | "deleting" | "deletion_failed";
  created_at: string;
  updated_at: string;
}

export interface DataSource {
  source_id: string;
  name: string;
  source_type: string;
  status: string;
  provenance: string;
  original_filename: string;
  size_bytes: number;
  table_count: number;
  view_count: number;
  created_at: string;
}

export interface MapLocation {
  location_id: string;
  label: string;
  latitude: number;
  longitude: number;
  place_id?: string | null;
  source: Record<string, unknown>;
}

export interface MapRoute {
  route_id: string;
  origin_location_id?: string | null;
  destination_location_id?: string | null;
  waypoint_location_ids: string[];
  encoded_polyline?: string | null;
  distance_meters?: number | null;
  duration_seconds?: number | null;
  resource_id?: string | null;
}

export interface MapAssignment {
  task_id: string;
  resource_id: string;
  sequence: number;
  start_at?: string | null;
  end_at?: string | null;
  origin_location_id?: string | null;
  destination_location_id?: string | null;
  travel_distance_meters?: number | null;
  travel_duration_seconds?: number | null;
}

export type MapAvailability = Record<
  "locations" | "routes" | "assignments" | "metrics" | "validation",
  "not_requested" | "available" | "unavailable" | "not_applicable"
>;

export interface MissionMapState {
  revision: number;
  updated_at: string;
  is_final: boolean;
  availability: MapAvailability;
  availability_reasons?: Partial<Record<keyof MapAvailability, string>>;
  locations: MapLocation[];
  routes: MapRoute[];
  assignments: MapAssignment[];
  metrics?: Record<string, unknown> | null;
  validation?: {
    feasible?: boolean | null;
    hard_violations?: Array<Record<string, unknown>>;
    warnings?: Array<Record<string, unknown>>;
  } | null;
  warnings: Array<Record<string, unknown>>;
}

export interface Clarification {
  question: string;
  reason: string;
  status: "open" | "answered";
  requested_at: string;
  answer?: string | null;
  answered_at?: string | null;
}

export interface ObjectiveDecision {
  proposed_objective: string;
  reason: string;
  hard_violations: Array<Record<string, unknown>>;
  status: "pending" | "accepted";
  requested_at: string;
  accepted_at?: string | null;
}

export interface Mission {
  mission_id: string;
  workspace_id: string;
  objective: string;
  authorized_source_ids: string[];
  status: MissionStatus;
  name?: string | null;
  summary?: string | null;
  clarification?: Clarification | null;
  objective_decision?: ObjectiveDecision | null;
  plan?: Record<string, unknown> | null;
  map_state?: MissionMapState | null;
  error?: string | null;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface MissionEvent {
  event_id: string;
  mission_id: string;
  type: string;
  agent?: string | null;
  tool?: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface MissionMapResponse {
  mission_id: string;
  status: MissionStatus;
  map_state: MissionMapState;
}

export interface WorkspaceMapMissionSummary {
  mission_id: string;
  name?: string | null;
  status: MissionStatus;
  map_revision: number;
  updated_at: string;
  is_final: boolean;
  locations: MapLocation[];
}

export interface WorkspaceQuestionHistoryMessage {
  role: "user" | "assistant";
  content: string;
}

export interface WorkspaceQuestionReference {
  mission_id: string;
  mission_name?: string | null;
  event_ids: string[];
}

export interface WorkspaceQuestionAnswer {
  answer: string;
  references: WorkspaceQuestionReference[];
}
