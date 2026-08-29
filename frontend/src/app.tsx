import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, GeoAgentApiError } from "./api";
import {
  describeEvent,
  formatDistance,
  formatDuration,
  formatTime,
  hasEventDetails,
  humanize,
  isAgentEvent,
  isLifecycleEvent,
  statusLabel,
} from "./display";
import { MapCanvas } from "./map-canvas";
import { missionDataStatus, type DataCategory } from "./mission-data-status";
import type {
  DataSource,
  MapAssignment,
  MapLocation,
  Mission,
  MissionEvent,
  MissionMapState,
  Workspace,
  WorkspaceMapMissionSummary,
} from "./types";

type PanelTab = "plan" | "agents" | "history";

const terminalStatuses = new Set(["completed", "failed", "awaiting_input", "awaiting_objective_decision"]);
const mapCategories: ReadonlyArray<readonly [DataCategory, string]> = [
  ["locations", "Locations"],
  ["routes", "Routes"],
  ["validation", "Validation"],
];
const mapGeographyCategories: ReadonlyArray<readonly [DataCategory, string]> = [
  ["locations", "Locations"],
  ["routes", "Routes"],
];

function errorMessage(error: unknown) {
  return error instanceof GeoAgentApiError || error instanceof Error
    ? error.message
    : "GeoAgent could not complete this request.";
}

function metricNumber(state: MissionMapState | null, key: string) {
  const value = state?.metrics?.[key];
  return typeof value === "number" ? value : undefined;
}

function availableMetric(state: MissionMapState | null, key: string) {
  return state?.availability.metrics === "available" ? metricNumber(state, key) : undefined;
}

function assignmentCount(state: MissionMapState | null) {
  if (state?.availability.assignments !== "available") return undefined;
  return metricNumber(state, "assigned_task_count") ?? state.assignments.length;
}

function mapStatusSummary(state: MissionMapState | null, status: Mission["status"], events: MissionEvent[]) {
  return mapGeographyCategories
    .filter(([category]) => state?.availability[category] !== "available")
    .map(([category, label]) => `${label}: ${missionDataStatus(category, state, status, events)}`);
}

function mapEmptyMessage(
  state: MissionMapState | null,
  status: Mission["status"] | undefined,
  events: MissionEvent[],
) {
  if (!state || !status) return "No real operational locations are available for this view yet.";
  if (state.availability.locations === "available") return "No usable real operational locations were returned for this Mission.";
  return `Locations: ${missionDataStatus("locations", state, status, events)}`;
}

function StatusBadge({ status }: { status: Mission["status"] }) {
  return <span className={`status-badge status-${status}`}>{statusLabel(status)}</span>;
}

function EmptyPanel({ children }: { children: ReactNode }) {
  return <div className="empty-panel">{children}</div>;
}

export function App() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [missions, setMissions] = useState<Mission[]>([]);
  const [sources, setSources] = useState<DataSource[]>([]);
  const [selectedMissionId, setSelectedMissionId] = useState<string | null>(null);
  const [selectedMission, setSelectedMission] = useState<Mission | null>(null);
  const [events, setEvents] = useState<MissionEvent[]>([]);
  const [mapState, setMapState] = useState<MissionMapState | null>(null);
  const [workspaceMap, setWorkspaceMap] = useState<WorkspaceMapMissionSummary[]>([]);
  const [activeTab, setActiveTab] = useState<PanelTab>("plan");
  const [newMissionOpen, setNewMissionOpen] = useState(false);
  const [newWorkspaceOpen, setNewWorkspaceOpen] = useState(false);
  const [workspaceSettingsOpen, setWorkspaceSettingsOpen] = useState(false);
  const [missionDeleteOpen, setMissionDeleteOpen] = useState(false);
  const [workspaceMenuOpen, setWorkspaceMenuOpen] = useState(false);
  const [workspaceDeleting, setWorkspaceDeleting] = useState(false);
  const [missionDeleting, setMissionDeleting] = useState(false);
  const [runPending, setRunPending] = useState(false);
  const [connection, setConnection] = useState<"checking" | "online" | "offline">("checking");
  const [notice, setNotice] = useState<string | null>(null);
  const [refreshIndex, setRefreshIndex] = useState(0);
  const [selectedLocation, setSelectedLocation] = useState<MapLocation | null>(null);
  const [highlightedResourceId, setHighlightedResourceId] = useState<string | null>(null);

  const refresh = useCallback(() => setRefreshIndex((value) => value + 1), []);

  useEffect(() => {
    let cancelled = false;
    void Promise.all([api.health(), api.workspaces()])
      .then(([health, response]) => {
        if (cancelled) return;
        setConnection(health.status === "ok" ? "online" : "offline");
        setWorkspaces(response.workspaces);
        setWorkspaceId((current) => current || response.workspaces[0]?.workspace_id || null);
      })
      .catch(() => {
        if (!cancelled) setConnection("offline");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!workspaceId) {
      setMissions([]);
      setSources([]);
      setWorkspaceMap([]);
      setSelectedMission(null);
      setEvents([]);
      setMapState(null);
      return;
    }
    let cancelled = false;
    const load = async () => {
      try {
        const [missionResponse, sourceResponse] = await Promise.all([
          api.missions(workspaceId),
          api.sources(workspaceId),
        ]);
        if (cancelled) return;
        setMissions(missionResponse.missions);
        setSources(sourceResponse.sources);
        if (selectedMissionId) {
          const [mission, eventResponse, mapResponse] = await Promise.all([
            api.mission(workspaceId, selectedMissionId),
            api.events(workspaceId, selectedMissionId),
            api.missionMap(workspaceId, selectedMissionId),
          ]);
          if (cancelled) return;
          setSelectedMission(mission);
          setEvents(eventResponse.events);
          setMapState(mapResponse.map_state);
          if (terminalStatuses.has(mission.status)) setRunPending(false);
        } else {
          const mapResponse = await api.workspaceMap(workspaceId);
          if (cancelled) return;
          setSelectedMission(null);
          setEvents([]);
          setMapState(null);
          setWorkspaceMap(mapResponse.missions);
        }
      } catch (error) {
        if (!cancelled) setNotice(errorMessage(error));
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [refreshIndex, selectedMissionId, workspaceId]);

  const selectedIsRunning = selectedMission?.status === "running";
  const workspaceHasRunningMission = missions.some((mission) => mission.status === "running");
  useEffect(() => {
    if (!workspaceId || !(runPending || selectedIsRunning || (!selectedMissionId && workspaceHasRunningMission))) return;
    const timer = window.setInterval(refresh, 1500);
    return () => window.clearInterval(timer);
  }, [refresh, runPending, selectedIsRunning, selectedMissionId, workspaceHasRunningMission, workspaceId]);

  const selectMission = (missionId: string | null) => {
    setSelectedMissionId(missionId);
    setSelectedLocation(null);
    setHighlightedResourceId(null);
    setActiveTab("plan");
  };

  const startMission = async (objective: string, sourceIds: string[]) => {
    if (!workspaceId) return;
    setNotice(null);
    try {
      const mission = await api.createMission(workspaceId, objective, sourceIds);
      setNewMissionOpen(false);
      setSelectedMissionId(mission.mission_id);
      setSelectedMission(mission);
      setRunPending(true);
      void api
        .runMission(workspaceId, mission.mission_id)
        .then((finishedMission) => {
          setSelectedMission(finishedMission);
          setRunPending(false);
          refresh();
        })
        .catch((error) => {
          setRunPending(false);
          setNotice(errorMessage(error));
          refresh();
        });
      refresh();
    } catch (error) {
      setNotice(errorMessage(error));
    }
  };

  const continueMission = (action: () => Promise<Mission>) => {
    setNotice(null);
    setRunPending(true);
    void action()
      .then((mission) => setSelectedMission(mission))
      .catch((error) => setNotice(errorMessage(error)))
      .finally(() => {
        setRunPending(false);
        refresh();
      });
  };

  const discardMission = () => {
    if (!workspaceId || !selectedMissionId) return;
    setNotice(null);
    void api
      .discardObjective(workspaceId, selectedMissionId)
      .then(() => {
        selectMission(null);
        refresh();
      })
      .catch((error) => setNotice(errorMessage(error)));
  };

  const createWorkspace = async (name: string) => {
    setNotice(null);
    try {
      const workspace = await api.createWorkspace(name);
      setWorkspaces((current) => [...current, workspace]);
      setWorkspaceId(workspace.workspace_id);
      selectMission(null);
      setNewWorkspaceOpen(false);
    } catch (error) {
      setNotice(errorMessage(error));
    }
  };

  const connectSource = async (name: string, file: File) => {
    if (!workspaceId) return;
    setNotice(null);
    try {
      await api.connectSqliteSource(workspaceId, name, file);
      setWorkspaceSettingsOpen(false);
      refresh();
    } catch (error) {
      setNotice(errorMessage(error));
    }
  };

  const deleteSelectedMission = async () => {
    if (!workspaceId || !selectedMissionId) return;
    setNotice(null);
    setMissionDeleting(true);
    try {
      await api.deleteMission(workspaceId, selectedMissionId);
      setMissionDeleteOpen(false);
      selectMission(null);
      refresh();
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setMissionDeleting(false);
    }
  };

  const deleteCurrentWorkspace = async (workspaceName: string) => {
    if (!workspaceId) return;
    setNotice(null);
    setWorkspaceDeleting(true);
    try {
      await api.deleteWorkspace(workspaceId, workspaceName);
      const response = await api.workspaces();
      const nextWorkspaceId = response.workspaces[0]?.workspace_id || null;
      setWorkspaces(response.workspaces);
      setWorkspaceId(nextWorkspaceId);
      setWorkspaceSettingsOpen(false);
      selectMission(null);
      refresh();
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setWorkspaceDeleting(false);
    }
  };

  const selectedWorkspace = workspaces.find((workspace) => workspace.workspace_id === workspaceId) || null;

  const activeMapLocations = useMemo(
    () => workspaceMap.flatMap((mission) => mission.locations),
    [workspaceMap],
  );
  const currentLocations = selectedMissionId ? mapState?.locations || [] : activeMapLocations;
  const currentRoutes = selectedMissionId ? mapState?.routes || [] : [];
  const currentAssignments = selectedMissionId ? mapState?.assignments || [] : [];
  const missingMapData = selectedMission ? mapStatusSummary(mapState, selectedMission.status, events) : [];

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand">Geo<span>Agent</span></div>
        <div className="workspace-control">
          <span>Workspace</span>
          <button className="workspace-trigger" aria-haspopup="menu" aria-expanded={workspaceMenuOpen} onClick={() => setWorkspaceMenuOpen((open) => !open)}>
            <strong>{selectedWorkspace?.name || "No workspaces"}</strong><span aria-hidden="true">⌄</span>
          </button>
          {workspaceMenuOpen && (
            <div className="workspace-menu" role="menu">
              {!workspaces.length && <span className="workspace-menu-empty">No workspaces</span>}
              {workspaces.map((workspace) => (
                <button key={workspace.workspace_id} role="menuitem" className={workspace.workspace_id === workspaceId ? "selected" : ""} onClick={() => {
                  setWorkspaceId(workspace.workspace_id);
                  setWorkspaceMenuOpen(false);
                  setWorkspaceSettingsOpen(false);
                  setMissionDeleteOpen(false);
                  setNotice(null);
                  setSelectedLocation(null);
                  setHighlightedResourceId(null);
                  setActiveTab("plan");
                  setSelectedMissionId(null);
                  refresh();
                }}>{workspace.name}</button>
              ))}
            </div>
          )}
        </div>
        <button className="header-action" onClick={() => setNewWorkspaceOpen(true)}>+ Workspace</button>
        <div className={`connection connection-${connection}`} title={`API ${connection}`}>
          <i /> {connection === "online" ? "API connected" : connection === "checking" ? "Checking API" : "API unavailable"}
        </div>
        <button className="primary-button" disabled={!workspaceId || !sources.length} title={!workspaceId ? "Create a Workspace first." : !sources.length ? "Connect a source before creating a Mission." : undefined} onClick={() => setNewMissionOpen(true)}>
          + New Mission
        </button>
      </header>

      {notice && (
        <div className="notice" role="alert">
          <span>{notice}</span>
          <button onClick={() => setNotice(null)} aria-label="Dismiss message">×</button>
        </div>
      )}

      <section className="command-grid">
        <aside className="mission-sidebar">
          {selectedWorkspace && (
            <div className="workspace-summary">
              <span className="eyebrow">Current workspace</span>
              <strong>{selectedWorkspace.name}</strong>
              <small>{missions.length} Mission{missions.length === 1 ? "" : "s"} · {sources.length} connected source{sources.length === 1 ? "" : "s"}</small>
              <button className="text-button" onClick={() => setWorkspaceSettingsOpen(true)}>Manage workspace</button>
            </div>
          )}
          <div className="section-heading"><span>{selectedWorkspace ? `Missions in ${selectedWorkspace.name}` : "Missions"}</span><button onClick={() => selectMission(null)}>All map</button></div>
          <div className="mission-list">
            {missions.map((mission) => (
              <button
                key={mission.mission_id}
                className={`mission-card ${mission.mission_id === selectedMissionId ? "selected" : ""}`}
                onClick={() => selectMission(mission.mission_id)}
              >
                <div className="mission-card-top"><StatusBadge status={mission.status} /><time>{formatTime(mission.updated_at, true)}</time></div>
                <strong>{mission.name || "Planning Mission"}</strong>
                <span>{mission.objective}</span>
                <div className="mission-card-meta">
                  {assignmentCount(mission.map_state || null) ?? "—"} assignments
                  <span>·</span>
                  {availableMetric(mission.map_state || null, "active_resource_count") ?? "—"} resources
                </div>
              </button>
            ))}
            {!workspaceId && <EmptyPanel><button className="primary-button" onClick={() => setNewWorkspaceOpen(true)}>Create your first Workspace</button></EmptyPanel>}
            {workspaceId && !sources.length && <EmptyPanel>Connect a SQLite source in <button className="inline-button" onClick={() => setWorkspaceSettingsOpen(true)}>Workspace settings</button> before starting a Mission.</EmptyPanel>}
            {workspaceId && !!sources.length && !missions.length && <EmptyPanel>Create a Mission to begin operational planning.</EmptyPanel>}
          </div>
        </aside>

        <section className="map-region">
          <div className="map-topline">
            <div>
              <span className="eyebrow">{selectedMission ? "Mission geography" : "All active Missions"}</span>
              <h1>{selectedMission?.name || selectedMission?.objective || "Live operational map"}</h1>
            </div>
            {selectedMission && <StatusBadge status={selectedMission.status} />}
          </div>
          <MapCanvas
            locations={currentLocations}
            routes={currentRoutes}
            assignments={currentAssignments}
            highlightedResourceId={highlightedResourceId}
            emptyMessage={mapEmptyMessage(mapState, selectedMission?.status, events)}
            onSelectLocation={setSelectedLocation}
          />
          {selectedLocation && (
            <div className="location-popover">
              <div><span className="eyebrow">Operational location</span><strong>{selectedLocation.label}</strong></div>
              <span>{selectedLocation.latitude.toFixed(5)}, {selectedLocation.longitude.toFixed(5)}</span>
              <button onClick={() => setSelectedLocation(null)}>×</button>
            </div>
          )}
          {selectedMission && mapState && !mapState.is_final && (
            <p className="map-progress">Map revision {mapState.revision} · Real tool results appear here as GeoAgent records them.</p>
          )}
          {!!missingMapData.length && <p className="map-data-notice">{missingMapData.join(" · ")}</p>}
        </section>

        <aside className="details-panel">
          {!selectedMission ? (
            <EmptyPanel>Select a Mission to inspect its plan, agents, and history.</EmptyPanel>
          ) : (
            <>
              <div className="detail-header"><div><span className="eyebrow">Mission intelligence</span><h2>{selectedMission.name || "Mission"}</h2></div><button className="mission-menu" onClick={() => setMissionDeleteOpen(true)} aria-label="Mission actions">•••</button></div>
              <div className="tabs" role="tablist">
                {(["plan", "agents", "history"] as PanelTab[]).map((tab) => (
                  <button key={tab} className={activeTab === tab ? "active" : ""} onClick={() => setActiveTab(tab)}>{tab}</button>
                ))}
              </div>
              {activeTab === "plan" && (
                <PlanPanel
                  mission={selectedMission}
                  mapState={mapState}
                  events={events}
                  highlightedResourceId={highlightedResourceId}
                  onHighlightResource={setHighlightedResourceId}
                  onAnswer={(answer) => workspaceId && continueMission(() => api.answerClarification(workspaceId, selectedMission.mission_id, answer))}
                  onAccept={() => workspaceId && continueMission(() => api.acceptObjective(workspaceId, selectedMission.mission_id))}
                  onDiscard={discardMission}
                />
              )}
              {activeTab === "agents" && <EventsPanel events={events} compact />}
              {activeTab === "history" && <EventsPanel events={events} />}
            </>
          )}
        </aside>
      </section>

      {newMissionOpen && (
        <NewMissionDialog sources={sources} onClose={() => setNewMissionOpen(false)} onStart={startMission} />
      )}
      {newWorkspaceOpen && <NewWorkspaceDialog onClose={() => setNewWorkspaceOpen(false)} onCreate={createWorkspace} />}
      {workspaceSettingsOpen && selectedWorkspace && <WorkspaceSettingsDialog workspace={selectedWorkspace} sources={sources} hasRunningMission={workspaceHasRunningMission} deleting={workspaceDeleting} onClose={() => setWorkspaceSettingsOpen(false)} onConnect={connectSource} onDelete={deleteCurrentWorkspace} />}
      {missionDeleteOpen && selectedMission && <MissionDeleteDialog mission={selectedMission} deleting={missionDeleting} onClose={() => setMissionDeleteOpen(false)} onDelete={deleteSelectedMission} />}
    </main>
  );
}

function PlanPanel({
  mission,
  mapState,
  events,
  highlightedResourceId,
  onHighlightResource,
  onAnswer,
  onAccept,
  onDiscard,
}: {
  mission: Mission;
  mapState: MissionMapState | null;
  events: MissionEvent[];
  highlightedResourceId: string | null;
  onHighlightResource: (resourceId: string | null) => void;
  onAnswer: (answer: string) => void;
  onAccept: () => void;
  onDiscard: () => void;
}) {
  const [answer, setAnswer] = useState("");
  const assignmentsByResource = useMemo(() => {
    const grouped = new Map<string, MapAssignment[]>();
    for (const assignment of mapState?.assignments || []) {
      grouped.set(assignment.resource_id, [...(grouped.get(assignment.resource_id) || []), assignment]);
    }
    return [...grouped.entries()].map(([resourceId, assignments]) => [resourceId, assignments.sort((a, b) => a.sequence - b.sequence)] as const);
  }, [mapState?.assignments]);
  const warnings = [...(mapState?.warnings || []), ...(mapState?.validation?.warnings || [])];

  if (mission.status === "awaiting_input" && mission.clarification?.status === "open") {
    return <div className="decision-panel"><span className="eyebrow">GeoAgent needs additional information</span><h3>{mission.clarification.question}</h3><p>{mission.clarification.reason}</p><textarea value={answer} onChange={(event) => setAnswer(event.target.value)} placeholder="Your answer" /><button className="primary-button" disabled={!answer.trim()} onClick={() => onAnswer(answer.trim())}>Continue Mission</button></div>;
  }
  if (mission.status === "awaiting_objective_decision" && mission.objective_decision?.status === "pending") {
    return <div className="decision-panel"><span className="eyebrow">Objective cannot currently be satisfied</span><h3>{mission.objective_decision.reason}</h3><p className="proposal">{mission.objective_decision.proposed_objective}</p><div className="button-row"><button className="primary-button" onClick={onAccept}>Accept &amp; Replan</button><button className="secondary-button" onClick={onDiscard}>Discard Mission</button></div></div>;
  }
  if (mission.status === "failed") {
    return <div className="panel-scroll"><EmptyPanel>{mission.error || "This Mission failed without a published plan."}</EmptyPanel><OperationalDataPanel mapState={mapState} missionStatus={mission.status} events={events} /></div>;
  }

  const totalDistance = availableMetric(mapState, "total_travel_distance_meters");
  const totalDuration = availableMetric(mapState, "total_travel_duration_seconds");

  return <div className="panel-scroll">
    <div className="plan-summary"><StatusBadge status={mission.status} /><p>{mission.summary || "GeoAgent is gathering and validating operational facts."}</p><div className="metric-grid"><Metric label="Assignments" value={assignmentCount(mapState)} status={missionDataStatus("assignments", mapState, mission.status, events)} /><Metric label="Resources" value={availableMetric(mapState, "active_resource_count")} status={missionDataStatus("metrics", mapState, mission.status, events)} /><Metric label="Distance" value={totalDistance === undefined ? undefined : formatDistance(totalDistance)} status={missionDataStatus("metrics", mapState, mission.status, events)} /><Metric label="Travel time" value={totalDuration === undefined ? undefined : formatDuration(totalDuration)} status={missionDataStatus("metrics", mapState, mission.status, events)} /></div></div>
    <OperationalDataPanel mapState={mapState} missionStatus={mission.status} events={events} />
    {assignmentsByResource.map(([resourceId, assignments]) => <AssignmentCard key={resourceId} resourceId={resourceId} assignments={assignments} active={highlightedResourceId === resourceId} onClick={() => onHighlightResource(highlightedResourceId === resourceId ? null : resourceId)} />)}
    {mapState?.availability.validation === "available" && mapState.validation && <section className="validation"><h3>Validation</h3><p className={mapState.validation.feasible ? "valid" : "invalid"}>{mapState.validation.feasible ? "Constraints validated" : "Validation has unresolved hard violations"}</p>{(mapState.validation.hard_violations || []).map((issue, index) => <p key={index}>{String(issue.message || issue.code || "Validation issue")}</p>)}</section>}
    {!!warnings.length && <section className="warnings"><h3>Warnings / risks</h3>{warnings.map((warning, index) => <p key={`${String(warning.code)}-${index}`}>{String(warning.message || warning.code || "Operational warning")}</p>)}</section>}
    {mission.plan && <details className="raw-plan"><summary>View JSON</summary><pre>{JSON.stringify(mission.plan, null, 2)}</pre></details>}
  </div>;
}

function Metric({ label, value, status }: { label: string; value: string | number | undefined; status: string }) {
  return <div><span>{label}</span><strong>{value ?? "—"}</strong>{value === undefined && <small>{status}</small>}</div>;
}

function operationalDataValue(category: DataCategory, mapState: MissionMapState | null) {
  if (!mapState || mapState.availability[category] !== "available") return undefined;
  if (category === "locations") {
    const count = mapState.locations.length;
    return count ? `${count} resolved — shown on the map` : "No usable locations returned";
  }
  if (category === "routes") {
    const count = mapState.routes.length;
    return count ? `${count} computed — shown on the map` : "No usable routes returned";
  }
  if (category === "validation") {
    if (!mapState.validation) return "Validation result returned";
    const violations = mapState.validation.hard_violations?.length || 0;
    return mapState.validation.feasible ? `Feasible — ${violations} hard violations` : `${violations} hard violations`;
  }
  return undefined;
}

function OperationalDataPanel({ mapState, missionStatus, events }: { mapState: MissionMapState | null; missionStatus: Mission["status"]; events: MissionEvent[] }) {
  return <section className="data-availability"><h3>Operational data</h3><div>{mapCategories.map(([category, label]) => {
    const value = operationalDataValue(category, mapState);
    const availability = mapState?.availability[category] || "not_requested";
    return <p key={category} className={`availability-${availability}`}><strong>{label}</strong><span>{value || missionDataStatus(category, mapState, missionStatus, events)}</span></p>;
  })}</div></section>;
}

function AssignmentCard({ resourceId, assignments, active, onClick }: { resourceId: string; assignments: MapAssignment[]; active: boolean; onClick: () => void }) {
  return <button className={`assignment-card ${active ? "active" : ""}`} onClick={onClick}><div className="assignment-header"><span className="eyebrow">Resource</span><strong>{resourceId}</strong></div>{assignments.map((assignment) => <div className="assignment-stop" key={assignment.task_id}><span>{assignment.sequence}</span><div><strong>{assignment.task_id}</strong><small>{formatTime(assignment.start_at)} · {formatDistance(assignment.travel_distance_meters)}</small></div></div>)}</button>;
}

function EventsPanel({ events, compact = false }: { events: MissionEvent[]; compact?: boolean }) {
  const visibleEvents = events.filter(compact ? isAgentEvent : isLifecycleEvent);
  const emptyMessage = compact
    ? "No safe delegation or tool events have been recorded yet."
    : "No Mission lifecycle or plan events have been recorded yet.";
  if (!visibleEvents.length) return <EmptyPanel>{emptyMessage}</EmptyPanel>;
  return <div className="event-list">{visibleEvents.map((event) => <article className="event-row" key={event.event_id}><time>{formatTime(event.created_at)}</time><div><strong>{humanize(event.agent || event.tool || "Mission Manager")}</strong><span>{describeEvent(event)}</span>{hasEventDetails(event) && <details className="event-details"><summary>Details</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>}</div></article>)}</div>;
}

function NewMissionDialog({ sources, onClose, onStart }: { sources: DataSource[]; onClose: () => void; onStart: (objective: string, sourceIds: string[]) => void }) {
  const [objective, setObjective] = useState("");
  const [limitSources, setLimitSources] = useState(false);
  const [selectedSourceIds, setSelectedSourceIds] = useState<string[]>([]);
  const toggleSource = (sourceId: string) => setSelectedSourceIds((current) => current.includes(sourceId) ? current.filter((id) => id !== sourceId) : [...current, sourceId]);
  return <div className="modal-backdrop" role="presentation"><section className="mission-dialog" role="dialog" aria-modal="true" aria-label="Create Mission"><div className="dialog-header"><div><span className="eyebrow">Create Mission</span><h2>What needs to be accomplished?</h2></div><button onClick={onClose}>×</button></div><textarea autoFocus value={objective} onChange={(event) => setObjective(event.target.value)} placeholder="Plan tomorrow's deliveries." maxLength={4000} /><label className="source-toggle"><input type="checkbox" checked={limitSources} onChange={(event) => setLimitSources(event.target.checked)} /> Limit this Mission to selected sources</label>{limitSources && <div className="source-list">{sources.map((source) => <label key={source.source_id}><input type="checkbox" checked={selectedSourceIds.includes(source.source_id)} onChange={() => toggleSource(source.source_id)} /> {source.name}</label>)}</div>}<p className="source-help">{limitSources ? "Only selected connected sources will be authorized." : `All ${sources.length} connected source${sources.length === 1 ? "" : "s"} will be authorized.`}</p><button className="primary-button full-width" disabled={!objective.trim() || (limitSources && !selectedSourceIds.length)} onClick={() => onStart(objective.trim(), limitSources ? selectedSourceIds : [])}>Start Mission</button></section></div>;
}

function NewWorkspaceDialog({ onClose, onCreate }: { onClose: () => void; onCreate: (name: string) => void }) {
  const [name, setName] = useState("");
  return <div className="modal-backdrop" role="presentation"><section className="mission-dialog" role="dialog" aria-modal="true" aria-label="Create Workspace"><div className="dialog-header"><div><span className="eyebrow">New workspace</span><h2>Create an operational home</h2></div><button onClick={onClose} aria-label="Close">×</button></div><p className="dialog-copy">A Workspace owns its connected data sources and contains its independent Missions.</p><label className="field-label">Workspace name<input autoFocus value={name} onChange={(event) => setName(event.target.value)} placeholder="Operations" maxLength={100} /></label><button className="primary-button full-width" disabled={!name.trim()} onClick={() => onCreate(name.trim())}>Create Workspace</button></section></div>;
}

function WorkspaceSettingsDialog({ workspace, sources, hasRunningMission, deleting, onClose, onConnect, onDelete }: { workspace: Workspace; sources: DataSource[]; hasRunningMission: boolean; deleting: boolean; onClose: () => void; onConnect: (name: string, file: File) => void; onDelete: (workspaceName: string) => void }) {
  const [sourceName, setSourceName] = useState("");
  const [sourceFile, setSourceFile] = useState<File | null>(null);
  const [confirmation, setConfirmation] = useState("");
  return <div className="modal-backdrop" role="presentation"><section className="mission-dialog workspace-dialog" role="dialog" aria-modal="true" aria-label="Workspace settings" aria-busy={deleting}><div className="dialog-header"><div><span className="eyebrow">Workspace settings</span><h2>{workspace.name}</h2></div><button onClick={onClose} disabled={deleting} aria-label="Close">×</button></div><section className="settings-section"><h3>Connected data sources</h3>{sources.length ? <div className="source-list source-records">{sources.map((source) => <div key={source.source_id} className="source-record"><div><strong>{source.name}</strong><small>SQLite · {source.original_filename} · {source.table_count} tables</small></div><span className="status-badge status-running">Connected</span></div>)}</div> : <p className="dialog-copy">No sources are connected yet. Connect one before creating a Mission.</p>}<label className="field-label">Source name<input disabled={deleting} value={sourceName} onChange={(event) => setSourceName(event.target.value)} placeholder="Operations database" maxLength={100} /></label><label className="field-label">SQLite file<input disabled={deleting} type="file" accept=".db,.sqlite,.sqlite3,application/vnd.sqlite3,application/x-sqlite3" onChange={(event) => setSourceFile(event.target.files?.[0] || null)} /></label><p className="source-help">Supported: .db, .sqlite, and .sqlite3. The backend validates the file before connecting it.</p><button className="secondary-button full-width" disabled={deleting || !sourceName.trim() || !sourceFile} onClick={() => sourceFile && onConnect(sourceName.trim(), sourceFile)}>Connect data source</button></section><section className="settings-section danger-zone"><h3>Delete Workspace</h3><p>Deletes this Workspace, its connected source records and files, every Mission, map state, and safe activity history.</p>{hasRunningMission ? <p className="danger-blocked">A running Mission prevents deletion. Wait until it pauses or finishes.</p> : <><label className="field-label">Type <strong>{workspace.name}</strong> to confirm<input disabled={deleting} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label><button className="danger-button full-width" disabled={deleting || confirmation !== workspace.name} onClick={() => onDelete(confirmation)}>{deleting ? <><span className="button-spinner" />Deleting Workspace…</> : "Delete Workspace"}</button>{deleting && <p className="deletion-progress">Removing Workspace records, connected sources, Missions, and history. Do not close this window.</p>}</>}</section></section></div>;
}

function MissionDeleteDialog({ mission, deleting, onClose, onDelete }: { mission: Mission; deleting: boolean; onClose: () => void; onDelete: () => void }) {
  const running = mission.status === "running";
  return <div className="modal-backdrop" role="presentation"><section className="mission-dialog danger-dialog" role="dialog" aria-modal="true" aria-label="Delete Mission" aria-busy={deleting}><div className="dialog-header"><div><span className="eyebrow">Mission actions</span><h2>{running ? "Mission is running" : "Delete Mission"}</h2></div><button onClick={onClose} disabled={deleting} aria-label="Close">×</button></div>{running ? <p className="dialog-copy">This Mission cannot be deleted while its agents are running. Wait for it to pause or finish.</p> : <><p className="dialog-copy"><strong>{mission.name || mission.objective}</strong>, its plan, map state, and safe activity history will be permanently removed from this Workspace.</p><div className="button-row"><button className="secondary-button" disabled={deleting} onClick={onClose}>Cancel</button><button className="danger-button" disabled={deleting} onClick={onDelete}>{deleting ? <><span className="button-spinner" />Deleting Mission…</> : "Delete Mission"}</button></div>{deleting && <p className="deletion-progress">Removing the Mission, map state, and safe activity history. Do not close this window.</p>}</>}</section></div>;
}
