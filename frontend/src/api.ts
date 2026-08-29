import type {
  DataSource,
  Mission,
  MissionEvent,
  MissionMapResponse,
  Workspace,
  WorkspaceMapMissionSummary,
} from "./types";

const baseUrl = (import.meta.env.VITE_API_BASE_URL || "http://localhost:8000").replace(
  /\/$/,
  "",
);

export class GeoAgentApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const isFormData = init?.body instanceof FormData;
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: {
      ...(init?.body && !isFormData ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (response.status === 204) return undefined as T;
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data as { detail?: { message?: string; code?: string } } | null;
    throw new GeoAgentApiError(
      detail?.detail?.message || "GeoAgent could not complete this request.",
      response.status,
      detail?.detail?.code,
    );
  }
  return data as T;
}

const missionPath = (workspaceId: string, missionId: string) =>
  `/api/workspaces/${encodeURIComponent(workspaceId)}/missions/${encodeURIComponent(missionId)}`;

export const api = {
  health: () => request<{ status: string }>("/health"),
  workspaces: () => request<{ workspaces: Workspace[] }>("/api/workspaces"),
  createWorkspace: (name: string) =>
    request<Workspace>("/api/workspaces", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  deleteWorkspace: (workspaceId: string, workspaceName: string) =>
    request<void>(`/api/workspaces/${encodeURIComponent(workspaceId)}`, {
      method: "DELETE",
      body: JSON.stringify({ workspace_name: workspaceName }),
    }),
  sources: (workspaceId: string) =>
    request<{ sources: DataSource[] }>(`/api/workspaces/${encodeURIComponent(workspaceId)}/data-sources`),
  connectSqliteSource: (workspaceId: string, name: string, file: File) => {
    const form = new FormData();
    form.append("name", name);
    form.append("file", file);
    return request<DataSource>(`/api/workspaces/${encodeURIComponent(workspaceId)}/data-sources/sqlite`, {
      method: "POST",
      body: form,
    });
  },
  missions: (workspaceId: string) =>
    request<{ missions: Mission[] }>(`/api/workspaces/${encodeURIComponent(workspaceId)}/missions`),
  mission: (workspaceId: string, missionId: string) => request<Mission>(missionPath(workspaceId, missionId)),
  events: (workspaceId: string, missionId: string) =>
    request<{ events: MissionEvent[] }>(`${missionPath(workspaceId, missionId)}/events`),
  missionMap: (workspaceId: string, missionId: string) =>
    request<MissionMapResponse>(`${missionPath(workspaceId, missionId)}/map`),
  workspaceMap: (workspaceId: string, includeCompleted = false) =>
    request<{ missions: WorkspaceMapMissionSummary[] }>(
      `/api/workspaces/${encodeURIComponent(workspaceId)}/map?include_completed=${includeCompleted}`,
    ),
  createMission: (workspaceId: string, objective: string, sourceIds: string[]) =>
    request<Mission>(`/api/workspaces/${encodeURIComponent(workspaceId)}/missions`, {
      method: "POST",
      body: JSON.stringify({ objective, source_ids: sourceIds }),
    }),
  deleteMission: (workspaceId: string, missionId: string) =>
    request<void>(missionPath(workspaceId, missionId), { method: "DELETE" }),
  runMission: (workspaceId: string, missionId: string) =>
    request<Mission>(`${missionPath(workspaceId, missionId)}/run`, { method: "POST" }),
  answerClarification: (workspaceId: string, missionId: string, answer: string) =>
    request<Mission>(`${missionPath(workspaceId, missionId)}/responses`, {
      method: "POST",
      body: JSON.stringify({ answer }),
    }),
  acceptObjective: (workspaceId: string, missionId: string) =>
    request<Mission>(`${missionPath(workspaceId, missionId)}/objective-decision/accept`, {
      method: "POST",
    }),
  discardObjective: (workspaceId: string, missionId: string) =>
    request<void>(`${missionPath(workspaceId, missionId)}/objective-decision`, { method: "DELETE" }),
};
