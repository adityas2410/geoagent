// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { AskGeoAgent, boundedQuestionHistory } from "./ask-geoagent";

describe("Ask GeoAgent", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("opens, sends, renders evidence, and preserves conversation when minimized", async () => {
    vi.spyOn(api, "askWorkspace").mockResolvedValue({
      answer: "Dispatch is running.",
      references: [{ mission_id: "msn_1", mission_name: "Dispatch", event_ids: ["evt_1"] }],
    });
    render(<AskGeoAgent workspaceId="ws_1" workspaceName="Operations" />);

    fireEvent.click(screen.getByRole("button", { name: "Open Ask GeoAgent" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Question for Ask GeoAgent" }), {
      target: { value: "What is running?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send question" }));

    expect(await screen.findByText("Dispatch is running.")).toBeTruthy();
    expect(screen.getByText("Dispatch · 1 event")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Minimize Ask GeoAgent" }));
    expect(screen.queryByText("Dispatch is running.")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open Ask GeoAgent" }));
    expect(screen.getByText("Dispatch is running.")).toBeTruthy();
  });

  it("shows loading, prevents duplicate sends, and submits Enter", async () => {
    let resolveAnswer: ((value: { answer: string; references: [] }) => void) | undefined;
    const pending = new Promise<{ answer: string; references: [] }>((resolve) => {
      resolveAnswer = resolve;
    });
    const ask = vi.spyOn(api, "askWorkspace").mockReturnValue(pending);
    render(<AskGeoAgent workspaceId="ws_1" workspaceName="Operations" />);

    fireEvent.click(screen.getByRole("button", { name: "Open Ask GeoAgent" }));
    const input = screen.getByRole("textbox", { name: "Question for Ask GeoAgent" });
    fireEvent.change(input, { target: { value: "Compare Missions" } });
    fireEvent.keyDown(input, { key: "Enter", shiftKey: false });

    expect(screen.getByText("Reviewing current Mission records")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Send question" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Send question" }));
    expect(ask).toHaveBeenCalledTimes(1);
    resolveAnswer?.({ answer: "Comparison complete.", references: [] });
    expect(await screen.findByText("Comparison complete.")).toBeTruthy();
  });

  it("clears conversation when the Workspace changes and uses no browser storage", async () => {
    const storageRead = vi.spyOn(Storage.prototype, "getItem");
    const storageWrite = vi.spyOn(Storage.prototype, "setItem");
    vi.spyOn(api, "askWorkspace").mockResolvedValue({ answer: "Current answer.", references: [] });
    const view = render(<AskGeoAgent workspaceId="ws_1" workspaceName="First" />);
    fireEvent.click(screen.getByRole("button", { name: "Open Ask GeoAgent" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Question for Ask GeoAgent" }), {
      target: { value: "Status?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send question" }));
    expect(await screen.findByText("Current answer.")).toBeTruthy();

    view.rerender(<AskGeoAgent workspaceId="ws_2" workspaceName="Second" />);
    await waitFor(() => expect(screen.queryByText("Current answer.")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Open Ask GeoAgent" }));
    expect(screen.getByText("Workspace operations Q&A")).toBeTruthy();
    expect(storageRead).not.toHaveBeenCalled();
    expect(storageWrite).not.toHaveBeenCalled();
  });

  it("shows a recoverable request error", async () => {
    vi.spyOn(api, "askWorkspace").mockRejectedValue(new Error("network detail"));
    render(<AskGeoAgent workspaceId="ws_1" workspaceName="Operations" />);
    fireEvent.click(screen.getByRole("button", { name: "Open Ask GeoAgent" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Question for Ask GeoAgent" }), {
      target: { value: "Status?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send question" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Ask GeoAgent could not answer right now.");
    expect((screen.getByRole("textbox", { name: "Question for Ask GeoAgent" }) as HTMLTextAreaElement).disabled).toBe(false);
  });
});

describe("boundedQuestionHistory", () => {
  it("keeps only the latest twenty messages within the character budget", () => {
    const messages = Array.from({ length: 25 }, (_, index) => ({
      id: String(index),
      role: index % 2 ? "assistant" as const : "user" as const,
      content: `message-${index}`,
    }));
    const result = boundedQuestionHistory(messages);
    expect(result).toHaveLength(20);
    expect(result[0].content).toBe("message-5");

    const oversized = boundedQuestionHistory([
      { id: "1", role: "user", content: "x".repeat(30_000) },
      { id: "2", role: "assistant", content: "y".repeat(3_000) },
    ]);
    expect(oversized).toEqual([{ role: "assistant", content: "y".repeat(3_000) }]);
  });
});
