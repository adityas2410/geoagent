import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, GeoAgentApiError } from "./api";
import type { WorkspaceQuestionHistoryMessage, WorkspaceQuestionReference } from "./types";

const MAX_HISTORY_MESSAGES = 20;
const MAX_HISTORY_CHARACTERS = 32_000;

interface ChatMessage extends WorkspaceQuestionHistoryMessage {
  id: string;
  references?: WorkspaceQuestionReference[];
}

export function boundedQuestionHistory(messages: ChatMessage[]): WorkspaceQuestionHistoryMessage[] {
  const selected: WorkspaceQuestionHistoryMessage[] = [];
  let characters = 0;
  for (let index = messages.length - 1; index >= 0 && selected.length < MAX_HISTORY_MESSAGES; index -= 1) {
    const message = messages[index];
    if (characters + message.content.length > MAX_HISTORY_CHARACTERS) break;
    selected.unshift({ role: message.role, content: message.content });
    characters += message.content.length;
  }
  return selected;
}

function errorMessage(error: unknown): string {
  if (error instanceof GeoAgentApiError) return error.message;
  return "Ask GeoAgent could not answer right now.";
}

function RadarIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="8" />
      <circle cx="12" cy="12" r="3" />
      <path d="M12 12 18 7M12 2v2M22 12h-2M12 22v-2M2 12h2" />
    </svg>
  );
}

export function AskGeoAgent({ workspaceId, workspaceName }: { workspaceId: string | null; workspaceName: string | null }) {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const conversationRef = useRef<HTMLDivElement | null>(null);
  const previousWorkspaceRef = useRef<string | null>(workspaceId);

  useEffect(() => {
    if (previousWorkspaceRef.current === workspaceId) return;
    previousWorkspaceRef.current = workspaceId;
    setOpen(false);
    setMessages([]);
    setDraft("");
    setLoading(false);
    setError(null);
  }, [workspaceId]);

  useEffect(() => {
    if (!open) return;
    conversationRef.current?.scrollTo?.({ top: conversationRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, loading, open]);

  const send = async () => {
    const question = draft.trim();
    if (!workspaceId || !question || loading) return;
    const history = boundedQuestionHistory(messages);
    const userMessage: ChatMessage = { id: crypto.randomUUID(), role: "user", content: question };
    setMessages((current) => [...current, userMessage]);
    setDraft("");
    setError(null);
    setLoading(true);
    try {
      const response = await api.askWorkspace(workspaceId, question, history);
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: response.answer,
          references: response.references,
        },
      ]);
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setLoading(false);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void send();
    }
  };

  return (
    <div className="ask-geoagent">
      {open && (
        <section className="ask-window" role="dialog" aria-label="Ask GeoAgent">
          <header className="ask-header">
            <span className="ask-header-icon"><RadarIcon /></span>
            <div><strong>Ask GeoAgent</strong><small>{workspaceName || "No active Workspace"}</small></div>
            <button type="button" onClick={() => setOpen(false)} aria-label="Minimize Ask GeoAgent">−</button>
          </header>
          <div className="ask-conversation" ref={conversationRef} aria-live="polite">
            {!messages.length && (
              <div className="ask-empty">
                <strong>Workspace operations Q&amp;A</strong>
                <span>Ask about Mission status, plans, agent activity, problems, or comparisons.</span>
              </div>
            )}
            {messages.map((message) => (
              <article key={message.id} className={`ask-message ask-message-${message.role}`}>
                <span>{message.role === "user" ? "You" : "GeoAgent"}</span>
                {message.role === "assistant" ? (
                  <div className="ask-markdown"><Markdown remarkPlugins={[remarkGfm]}>{message.content}</Markdown></div>
                ) : <p>{message.content}</p>}
                {!!message.references?.length && (
                  <div className="ask-references" aria-label="Answer evidence">
                    {message.references.map((reference) => (
                      <span key={reference.mission_id} title={reference.event_ids.join(", ") || "Mission record"}>
                        {reference.mission_name || reference.mission_id}
                        {reference.event_ids.length ? ` · ${reference.event_ids.length} event${reference.event_ids.length === 1 ? "" : "s"}` : ""}
                      </span>
                    ))}
                  </div>
                )}
              </article>
            ))}
            {loading && <div className="ask-thinking"><i /><i /><i /><span>Reviewing current Mission records</span></div>}
            {error && <p className="ask-error" role="alert">{error}</p>}
          </div>
          <div className="ask-composer">
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKeyDown}
              placeholder="Ask about this Workspace…"
              aria-label="Question for Ask GeoAgent"
              maxLength={4000}
              disabled={!workspaceId || loading}
            />
            <button type="button" onClick={() => void send()} disabled={!workspaceId || !draft.trim() || loading} aria-label="Send question">
              <span aria-hidden="true">➤</span>
            </button>
          </div>
        </section>
      )}
      {!open && (
        <button
          type="button"
          className="ask-launcher"
          onClick={() => setOpen(true)}
          disabled={!workspaceId}
          title={workspaceId ? "Ask about Workspace operations" : "Create a Workspace to use Ask GeoAgent"}
          aria-label="Open Ask GeoAgent"
        >
          <RadarIcon /><span>Ask GeoAgent</span>
        </button>
      )}
    </div>
  );
}
