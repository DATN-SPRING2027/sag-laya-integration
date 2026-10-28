// @vitest-environment jsdom
import * as React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import enMessages from "@/messages/en-US.json";
import { MarkdownContent } from "@/components/features/markdown-content";
import { CitationBlock } from "@/components/features/chat/citation-block";
import { AgentActivityTimeline } from "@/components/features/chat/agent-activity-timeline";
import { SessionContextInspector } from "@/components/features/chat/session-context-inspector";
import {
  buildDecisionLineage,
  filterAnswerSessionCitations,
  normalizeSessionContextAudit,
  resolveSessionCitations,
} from "@/lib/session-context-presentation";
import type { Citation, SessionCitation } from "@/lib/types";

// Enable React 19 act environment in jsdom
(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

function mountWithIntl(element: React.ReactElement) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => {
    root.render(
      <NextIntlClientProvider locale="en-US" messages={enMessages}>
        {element}
      </NextIntlClientProvider>,
    );
  });
  return {
    container,
    unmount() {
      act(() => root.unmount());
      container.remove();
    },
  };
}

afterEach(() => {
  document.body.innerHTML = "";
});

const DOC_CITATIONS: Citation[] = [
  {
    n: 1,
    kind: "internal",
    chunk_id: "chunk_1",
    source_id: "src_1",
    source_name: "System Spec",
    heading: "Vector Dimension Policy",
    snippet: "Embeddings must match collection dimension.",
    score: 0.91,
  },
];

const SESSION_CITATIONS: SessionCitation[] = [
  {
    ref: "S1",
    index: 1,
    kind: "evidence_group",
    primaryTurnId: "turn_3",
    includedTurnIds: ["turn_3"],
    epistemicStatus: "CONFIRMED",
    closureReason: "anaphoric_confirmation_closure",
    utilityScore: 0.92,
    redundancyPenalty: 0,
    coverageGain: 0.5,
    marginalGainPerToken: 0.031,
    tokenCost: 64,
    turns: [
      {
        id: "turn_3",
        role: "user",
        excerpt: "Confirmed: use 1024-dim Qwen3-Embedding-4B in Qdrant.",
        ordinal: 3,
        loaded: true,
      },
    ],
    mappedInAnswer: true,
  },
];

describe("Chặng 4 UI components (Dual Citations, Timeline & Context Inspector)", () => {
  it("renders interactive buttons for valid [1] and [S1] while leaving invalid [99]/[S99] and inline code untouched", () => {
    const onDocClick = vi.fn();
    const onSessionClick = vi.fn();

    const { container, unmount } = mountWithIntl(
      <MarkdownContent
        content="Policy [1] aligns with our confirmed decision [S1], ignores hallucinated [99] and [S99], and keeps `[S1]` as code."
        citations={DOC_CITATIONS}
        sessionCitations={SESSION_CITATIONS}
        onCitationClick={onDocClick}
        onSessionCitationClick={onSessionClick}
      />,
    );

    const buttons = Array.from(container.querySelectorAll("button"));
    expect(buttons.map((btn) => btn.textContent)).toEqual(["1", "S1"]);

    act(() => {
      buttons[0].click();
      buttons[1].click();
    });

    expect(onDocClick).toHaveBeenCalledTimes(1);
    expect(onDocClick.mock.calls[0][0].chunk_id).toBe("chunk_1");
    expect(onSessionClick).toHaveBeenCalledTimes(1);
    expect(onSessionClick.mock.calls[0][0].ref).toBe("S1");

    expect(container.textContent).toContain("[99]");
    expect(container.textContent).toContain("[S99]");
    expect(container.querySelector("code")?.textContent).toBe("[S1]");
    unmount();
  });

  it("renders compound citations [1, 2], [S1, S2], [1, S2] outside code and preserves recent_continuity in CitationBlock (PR #46 Finding 3)", () => {
    const onDocClick = vi.fn();
    const onSessionClick = vi.fn();

    const twoDocs: Citation[] = [
      ...DOC_CITATIONS,
      {
        n: 2,
        kind: "internal",
        chunk_id: "chunk_2",
        source_id: "src_2",
        source_name: "Migration ADR",
        heading: "Dual Write",
        snippet: "PostgreSQL + Qdrant sync.",
        score: 0.88,
      },
    ];

    const audit = normalizeSessionContextAudit({
      mode: "on",
      ablation: "V5_ecce_full",
      budget_tokens: 1600,
      used_evidence_tokens: 64,
      used_recent_tokens: 48,
      total_output_history_tokens: 112,
      selected_groups: [
        {
          citation_tag: "S1",
          primary_turn_id: "u1",
          included_turn_ids: ["u1"],
          closure_reason: "direct_match",
          epistemic_status: "CONFIRMED",
          utility_score: 0.92,
          redundancy_penalty: 0,
          coverage_gain: 0.5,
          marginal_gain_per_token: 0.03,
          token_cost: 64,
        },
      ],
      recent_turn_ids: ["u7", "u8"],
      rejected_candidates: [],
      latency_ms: 6.2,
    })!;

    const answerMarkdown =
      "Docs [1, 2], session [S1, S2], mixed [1, S2], prose interval [1, 100], partial [S1, S99], and code `[1, S1]`.";

    const resolvedSession = resolveSessionCitations({
      content: answerMarkdown,
      audit,
      messages: [
        { id: "u1", role: "user", content: "Confirmed Qdrant." },
        { id: "u7", role: "user", content: "Check latency SLA." },
        { id: "u8", role: "assistant", content: "Latency < 10ms." },
      ],
      sessionCitations: [
        {
          ref: "S1",
          primary_message_id: "u1",
          message_ids: ["u1"],
          turn_ordinals: [1],
          epistemic_status: "CONFIRMED",
        },
        {
          ref: "S2",
          primary_message_id: "u8",
          message_ids: ["u7", "u8"],
          turn_ordinals: [2, 3],
          epistemic_status: "FACT",
        },
      ],
    });

    const answerSession = filterAnswerSessionCitations(audit, resolvedSession);
    expect(answerSession.map((item) => item.ref)).toEqual(["S1", "S2"]);

    const { container, unmount } = mountWithIntl(
      <div>
        <MarkdownContent
          content={answerMarkdown}
          citations={twoDocs}
          sessionCitations={resolvedSession}
          onCitationClick={onDocClick}
          onSessionCitationClick={onSessionClick}
        />
        <CitationBlock
          citations={twoDocs}
          sessionCitations={answerSession}
          onSessionCitationClick={onSessionClick}
        />
      </div>,
    );

    const proseButtons = Array.from(
      container.querySelectorAll<HTMLButtonElement>(".answer-prose button"),
    );
    expect(proseButtons.map((btn) => btn.textContent)).toEqual([
      "1",
      "2",
      "S1",
      "S2",
      "1",
      "S2",
      "S1",
    ]);
    expect(container.textContent).toContain("[1, 100]");
    expect(container.textContent).toContain("[S99]");
    expect(container.querySelector("code")?.textContent).toBe("[1, S1]");
    expect(container.textContent).toContain("4 references");

    act(() => {
      proseButtons[3].click();
    });
    expect(onSessionClick).toHaveBeenCalledTimes(1);
    expect(onSessionClick.mock.calls[0][0].ref).toBe("S2");
    expect(onSessionClick.mock.calls[0][0].includedTurnIds).toEqual(["u7", "u8"]);
    unmount();
  });

  it("renders Dual Source Center in CitationBlock with both session evidence and internal sources", () => {
    const onSessionClick = vi.fn();
    const onInspect = vi.fn();

    const { container, unmount } = mountWithIntl(
      <CitationBlock
        citations={DOC_CITATIONS}
        sessionCitations={SESSION_CITATIONS}
        onSessionCitationClick={onSessionClick}
        onInspectContext={onInspect}
      />,
    );

    const toggleBtn = container.querySelector("button")!;
    expect(toggleBtn.textContent).toContain("2 references");

    act(() => {
      toggleBtn.click();
    });

    expect(container.textContent).toContain("Session history evidence");
    expect(container.textContent).toContain("Internal sources");
    expect(container.textContent).toContain("Confirmed");

    const jumpBtn = Array.from(container.querySelectorAll("button")).find((btn) =>
      btn.textContent?.includes("Jump to turn"),
    );
    expect(jumpBtn).toBeDefined();
    act(() => {
      jumpBtn!.click();
    });
    expect(onSessionClick).toHaveBeenCalledTimes(1);
    unmount();
  });

  it("renders dedicated session_context step in AgentActivityTimeline without inflating tool count", () => {
    const onSessionContextClick = vi.fn();
    const audit = normalizeSessionContextAudit({
      mode: "on",
      ablation: "V5_ecce_full",
      budget_tokens: 1600,
      used_evidence_tokens: 86,
      used_recent_tokens: 40,
      total_output_history_tokens: 126,
      selected_groups: [
        {
          citation_tag: "S1",
          primary_turn_id: "turn_3",
          included_turn_ids: ["turn_3"],
          closure_reason: "direct_match",
          epistemic_status: "CONFIRMED",
          utility_score: 0.9,
          redundancy_penalty: 0,
          coverage_gain: 0.5,
          marginal_gain_per_token: 0.02,
          token_cost: 86,
        },
      ],
      rejected_candidates: [],
      latency_ms: 8.4,
    })!;

    const { container, unmount } = mountWithIntl(
      <AgentActivityTimeline
        collapsed={false}
        onSessionContextClick={onSessionContextClick}
        steps={[
          {
            kind: "session_context",
            step: 0,
            name: "session_context",
            mode: "on",
            ms: 8.4,
            audit,
          },
          {
            kind: "tool",
            step: 1,
            name: "search_context",
            ms: 42,
            count: 3,
          },
        ]}
      />,
    );

    // Header counts only the 1 real tool operation, not session_context
    expect(container.textContent).toContain("Completed 1 tool operation");
    expect(container.textContent).toContain("Session context (ECCE)");
    expect(container.textContent).toContain("1 bundles · 126 tok");

    const ecceRow = Array.from(container.querySelectorAll("button")).find((btn) =>
      btn.textContent?.includes("Session context (ECCE)"),
    );
    expect(ecceRow).toBeDefined();
    act(() => {
      ecceRow!.click();
    });
    expect(onSessionContextClick).toHaveBeenCalledTimes(1);
    unmount();
  });

  it("renders SessionContextInspector KPIs, budget warning, rejected filter pills, and decision lineage", () => {
    const onJumpToTurn = vi.fn();
    const audit = normalizeSessionContextAudit({
      mode: "on",
      ablation: "V5_ecce_full",
      memory_scope: "hybrid_both",
      temporal_mode: "current_effective",
      router_model: "Kev-4b",
      router_fallback_used: false,
      total_indexed_turns: 18,
      episode_count: 4,
      active_decisions_count: 1,
      budget_tokens: 1200,
      used_evidence_tokens: 310,
      used_recent_tokens: 90,
      total_output_history_tokens: 400,
      llm_memory_calls: 0,
      partial_coverage: true,
      view_weights: { state: 0.4, graph: 0.3, hybrid: 0.2, hierarchy: 0.1 },
      selected_groups: [
        {
          citation_tag: "S1",
          primary_turn_id: "turn_3",
          included_turn_ids: ["turn_3"],
          closure_reason: "direct_match",
          epistemic_status: "CONFIRMED",
          utility_score: 0.93,
          redundancy_penalty: 0,
          coverage_gain: 0.55,
          marginal_gain_per_token: 0.03,
          token_cost: 310,
        },
      ],
      rejected_candidates: [
        {
          turn_id: "turn_1",
          turn_ordinal: 0,
          raw_score: 0.77,
          epistemic_status: "SUPERSEDED",
          reason_code: "SUPERSEDED_BY_NEWER_DECISION",
          detail: "Superseded by confirmed turn turn_3",
        },
        {
          turn_id: "turn_2",
          turn_ordinal: 1,
          raw_score: 0.39,
          epistemic_status: "FACT",
          reason_code: "TOKEN_BUDGET_CAP",
          detail: "Requires 190 tokens; remaining budget is 80",
        },
      ],
      latency_ms: 7.2,
      persistence_status: "hydrated_clean",
      persisted_units_written: 1,
      qdrant_points_upserted: 3,
    })!;

    const messages = [
      { id: "turn_1", role: "user" as const, content: "Initial proposal: use Redis." },
      { id: "turn_2", role: "assistant" as const, content: "Background context note." },
      { id: "turn_3", role: "user" as const, content: "Confirmed: migrate to Qdrant." },
      { id: "turn_4", role: "assistant" as const, content: "We use Qdrant [S1]." },
    ];
    const citations = resolveSessionCitations({
      content: messages[3].content,
      audit,
      messages,
      assistantMessageId: "turn_4",
    });
    const lineage = buildDecisionLineage({ currentAudit: audit, messages });

    const { container, unmount } = mountWithIntl(
      <SessionContextInspector
        audit={audit}
        sessionCitations={citations}
        decisionLineage={lineage}
        onJumpToTurn={onJumpToTurn}
      />,
    );

    expect(container.textContent).toContain("Hydrated clean (PG + Qdrant)");
    expect(container.textContent).toContain("400 / 1200 tok");
    expect(container.textContent).toContain("Partial coverage");
    expect(container.textContent).toContain("Decision ledger");

    // Switch to Rejected tab using Radix keyboard/mouse events
    const rejectedTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent?.includes("Rejected"),
    ) as HTMLElement;
    expect(rejectedTab).toBeDefined();
    act(() => {
      rejectedTab.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, button: 0 }));
      rejectedTab.click();
    });

    expect(container.textContent).toContain("Superseded by newer decision");
    expect(container.textContent).toContain("Token budget cap");

    // Click superseding jump action
    const jumpSuperseding = Array.from(container.querySelectorAll("button")).find((el) =>
      el.textContent?.includes("Jump to superseding turn"),
    );
    expect(jumpSuperseding).toBeDefined();
    act(() => {
      jumpSuperseding!.click();
    });
    expect(onJumpToTurn).toHaveBeenCalledWith("turn_3");

    // Switch to Lineage tab
    const lineageTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent?.includes("Lineage"),
    ) as HTMLElement;
    act(() => {
      lineageTab.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, button: 0 }));
      lineageTab.click();
    });

    expect(container.textContent).toContain("Decision supersession chain #1");
    expect(container.textContent).toContain("Initial proposal: use Redis.");
    expect(container.textContent).toContain("Confirmed: migrate to Qdrant.");
    unmount();
  });
});
