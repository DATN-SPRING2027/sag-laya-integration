import { describe, expect, it } from "vitest";

import { canLoadMoreSearch, resolveSearchRun } from "./search-state";

const initial = {
  requestedKey: "query",
  committedKey: "",
  committedTopK: 0,
  hasResult: false,
  idle: true,
};

describe("fixed search result limit", () => {
  it("uses five results and caps larger explicit requests", () => {
    expect(resolveSearchRun(initial)).toMatchObject({ automatic: false, topK: 5 });
    expect(resolveSearchRun({ ...initial, requestedTopK: 128 })).toMatchObject({
      automatic: false, topK: 5,
    });
    expect(resolveSearchRun({ ...initial, requestedTopK: 3 }).topK).toBe(5);
  });

  it("does not offer loading beyond five", () => {
    expect(resolveSearchRun({ ...initial, intent: "load-more", committedKey: "query",
      committedTopK: 5, hasResult: true })).toMatchObject({ automatic: false, topK: 5, intent: "replace" });
    expect(resolveSearchRun({ ...initial, intent: "load-more", requestedTopK: 128,
      committedKey: "old-query", hasResult: true }).automatic).toBe(false);
    expect(canLoadMoreSearch({ phase: "idle", hasResult: true, hasMore: true,
      topK: 5, committedKey: "query", draftKey: "query" })).toBe(false);
  });
});
