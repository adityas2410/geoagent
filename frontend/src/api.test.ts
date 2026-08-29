import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

describe("workspace management API client", () => {
  afterEach(() => vi.restoreAllMocks());

  it("uploads a SQLite file as multipart data without a JSON content type", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ source_id: "src_1" }), { status: 201 }),
    );
    const file = new File(["SQLite format 3"], "operations.db", { type: "application/vnd.sqlite3" });

    await api.connectSqliteSource("ws_1", "Operations", file);

    const [, init] = fetchMock.mock.calls[0];
    expect(init?.method).toBe("POST");
    expect(init?.body).toBeInstanceOf(FormData);
    expect(init?.headers).toEqual({});
  });

  it("sends the exact workspace confirmation before deletion", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));

    await api.deleteWorkspace("ws_1", "Operations");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain("/api/workspaces/ws_1");
    expect(init?.method).toBe("DELETE");
    expect(init?.body).toBe(JSON.stringify({ workspace_name: "Operations" }));
  });
});
