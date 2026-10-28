import { describe, expect, it } from "vitest";

import {
  buildDecisionLineage,
  computeBudgetBreakdown,
  extractAnswerSessionCitationRefs,
  filterAnswerSessionCitations,
  normalizeSessionContextAudit,
  normalizeViewWeights,
  parseSessionContextStep,
  resolveSessionCitations,
  type SessionTranscriptTurnLike,
} from "./session-context-presentation";
import type { MessageStep } from "./types";

const SAMPLE_AUDIT_RAW = {
  mode: "on",
  ablation: "V5_ecce_full",
  memory_scope: "hybrid_both",
  temporal_mode: "current_effective",
  router_model: "Kev-4b",
  router_fallback_used: false,
  total_indexed_turns: 14,
  episode_count: 3,
  active_decisions_count: 2,
  budget_tokens: 1600,
  used_evidence_tokens: 284,
  used_recent_tokens: 106,
  total_output_history_tokens: 390,
  llm_memory_calls: 0,
  partial_coverage: true,
  view_weights: {
    hybrid: 0.18,
    graph: 0.32,
    hierarchy: 0.16,
    state: 0.34,
  },
  selected_groups: [
    {
      citation_tag: "S1",
      primary_turn_id: "turn_3",
      included_turn_ids: ["turn_2", "turn_3"],
      closure_reason: "anaphoric_confirmation_closure",
      epistemic_status: "CONFIRMED",
      utility_score: 0.9124,
      redundancy_penalty: 0.0,
      coverage_gain: 0.5,
      marginal_gain_per_token: 0.0312,
      token_cost: 86,
    },
  ],
  rejected_candidates: [
    {
      turn_id: "turn_1",
      turn_ordinal: 0,
      raw_score: 0.7812,
      epistemic_status: "SUPERSEDED",
      reason_code: "SUPERSEDED_BY_NEWER_DECISION",
      detail: "Superseded by confirmed turn turn_3",
    },
    {
      turn_id: "turn_0",
      turn_ordinal: 1,
      raw_score: 0.412,
      epistemic_status: "FACT",
      reason_code: "TOKEN_BUDGET_CAP",
      detail: "Requires 210 tokens; remaining budget is 95",
    },
  ],
  latency_ms: 9.07,
  persistence_status: "hydrated_clean",
  persisted_units_written: 2,
  qdrant_points_upserted: 6,
};

describe("session-context-presentation", () => {
  it("normalizes audit payloads and safely rejects malformed inputs", () => {
    expect(normalizeSessionContextAudit(null)).toBeNull();
    expect(normalizeSessionContextAudit("invalid")).toBeNull();

    const step: MessageStep = {
      kind: "session_context",
      step: 0,
      name: "session_context",
      mode: "on",
      ms: 9.07,
      details: {
        scope: "knowledge",
        sources: [{ id: "src_adr", name: "Architecture ADR" }],
      },
      audit: SAMPLE_AUDIT_RAW,
    };

    const parsed = parseSessionContextStep([step]);
    expect(parsed).not.toBeNull();
    expect(parsed?.audit.mode).toBe("on");
    expect(parsed?.audit.persistence_status).toBe("hydrated_clean");
    expect(parsed?.audit.persisted_units_written).toBe(2);
    expect(parsed?.audit.qdrant_points_upserted).toBe(6);
    expect(parsed?.transitiveSources).toEqual([
      { id: "src_adr", name: "Architecture ADR" },
    ]);
  });

  it("extracts inline [S*] citations from prose while ignoring code blocks and inline code", () => {
    const markdown = [
      "Final architecture uses PostgreSQL + Qdrant [S1] and ADR v2 [1].",
      "Ignore inline code `[S2]` and fenced blocks:",
      "```json",
      '{ "example": "[S3]" }',
      "```",
      "Also keep continuity note [S4].",
    ].join("\n");

    const refs = extractAnswerSessionCitationRefs(markdown);
    expect([...refs]).toEqual(["S1", "S4"]);
  });

  it("resolves evidence groups, recent continuity [S{k+1}], and unloaded older turns", () => {
    const audit = normalizeSessionContextAudit(SAMPLE_AUDIT_RAW)!;
    const messages: SessionTranscriptTurnLike[] = [
      { id: "turn_2", role: "user", content: "Should we switch from Redis vector to Qdrant?" },
      { id: "turn_3", role: "user", content: "Confirmed: use Qdrant + PostgreSQL for session substrate." },
      { id: "turn_4", role: "assistant", content: "Noted, Qdrant + PostgreSQL is locked in." },
      { id: "turn_5", role: "user", content: "What storage stack did we confirm and what did we discuss just now?" },
      {
        id: "turn_6",
        role: "assistant",
        content: "We confirmed Qdrant + PostgreSQL [S1] and noted it in our latest exchange [S2].",
      },
    ];

    const resolved = resolveSessionCitations({
      content: messages[4].content,
      audit,
      messages,
      assistantMessageId: "turn_6",
    });

    expect(resolved).toHaveLength(2);
    expect(resolved[0].ref).toBe("S1");
    expect(resolved[0].kind).toBe("evidence_group");
    expect(resolved[0].epistemicStatus).toBe("CONFIRMED");
    expect(resolved[0].mappedInAnswer).toBe(true);
    expect(resolved[0].turns.map((t) => t.ordinal)).toEqual([1, 2]);

    expect(resolved[1].ref).toBe("S2");
    expect(resolved[1].kind).toBe("recent_continuity");
    expect(resolved[1].mappedInAnswer).toBe(true);
    expect(resolved[1].turns.some((t) => t.id === "turn_4")).toBe(true);

    // Shadow mode keeps citations out of the answer Reference Center while preserving Inspector data
    const shadowAudit = normalizeSessionContextAudit({ ...SAMPLE_AUDIT_RAW, mode: "shadow" })!;
    expect(filterAnswerSessionCitations(shadowAudit, resolved)).toEqual([]);
    expect(filterAnswerSessionCitations(audit, resolved)).toHaveLength(2);
  });

  it("reconstructs chronological decision superseding lineage across thread turns", () => {
    const earlierAudit = normalizeSessionContextAudit({
      ...SAMPLE_AUDIT_RAW,
      selected_groups: [
        {
          citation_tag: "S1",
          primary_turn_id: "turn_3",
          included_turn_ids: ["turn_3"],
          closure_reason: "direct_match",
          epistemic_status: "CONFIRMED",
          utility_score: 0.88,
          redundancy_penalty: 0,
          coverage_gain: 0.4,
          marginal_gain_per_token: 0.02,
          token_cost: 42,
        },
      ],
      rejected_candidates: [
        {
          turn_id: "turn_1",
          turn_ordinal: 0,
          raw_score: 0.72,
          epistemic_status: "SUPERSEDED",
          reason_code: "SUPERSEDED_BY_NEWER_DECISION",
          detail: "Superseded by confirmed turn turn_3",
        },
      ],
    })!;

    const latestAudit = normalizeSessionContextAudit({
      ...SAMPLE_AUDIT_RAW,
      selected_groups: [
        {
          citation_tag: "S1",
          primary_turn_id: "turn_5",
          included_turn_ids: ["turn_5"],
          closure_reason: "direct_match",
          epistemic_status: "CONFIRMED",
          utility_score: 0.94,
          redundancy_penalty: 0,
          coverage_gain: 0.5,
          marginal_gain_per_token: 0.03,
          token_cost: 50,
        },
      ],
      rejected_candidates: [
        {
          turn_id: "turn_3",
          turn_ordinal: 2,
          raw_score: 0.81,
          epistemic_status: "SUPERSEDED",
          reason_code: "SUPERSEDED_BY_NEWER_DECISION",
          detail: "Superseded by confirmed turn turn_5",
        },
      ],
    })!;

    const messages: SessionTranscriptTurnLike[] = [
      { id: "turn_1", role: "user", content: "Proposal v1: store vectors in SQLite." },
      { id: "turn_2", role: "assistant", content: "Recorded v1." },
      { id: "turn_3", role: "user", content: "Update v2: switch to pgvector." },
      {
        id: "turn_4",
        role: "assistant",
        content: "Updated to pgvector.",
        steps: [{ kind: "session_context", step: 0, audit: earlierAudit }],
      },
      { id: "turn_5", role: "user", content: "Final v3: confirm Qdrant + PostgreSQL." },
      {
        id: "turn_6",
        role: "assistant",
        content: "Confirmed Qdrant + PostgreSQL [S1].",
        steps: [{ kind: "session_context", step: 0, audit: latestAudit }],
      },
    ];

    const lineage = buildDecisionLineage({
      currentAudit: latestAudit,
      messages,
    });

    expect(lineage.supersededCount).toBe(2);
    expect(lineage.chains).toHaveLength(1);
    const chain = lineage.chains[0];
    expect(chain.hasSupersession).toBe(true);
    expect(chain.latestStatus).toBe("CONFIRMED");
    expect(chain.nodes.map((node) => node.turnId)).toEqual([
      "turn_1",
      "turn_3",
      "turn_5",
    ]);
    expect(chain.nodes.map((node) => node.epistemicStatus)).toEqual([
      "SUPERSEDED",
      "SUPERSEDED",
      "CONFIRMED",
    ]);
    expect(chain.nodes[2].citationTag).toBe("S1");
  });

  it("preserves both CONFIRMED supersession and OPEN_ISSUE when two bundles share a primary turn", () => {
    const audit = normalizeSessionContextAudit({
      ...SAMPLE_AUDIT_RAW,
      selected_groups: [
        {
          citation_tag: "S1",
          primary_turn_id: "turn_3",
          included_turn_ids: ["turn_3", "turn_4"],
          closure_reason: "decision_lineage_closure",
          epistemic_status: "CONFIRMED",
          utility_score: 0.95,
          redundancy_penalty: 0.02,
          coverage_gain: 0.9,
          marginal_gain_per_token: 0.01,
          token_cost: 96,
        },
        {
          citation_tag: "S2",
          primary_turn_id: "turn_3",
          included_turn_ids: ["turn_3"],
          closure_reason: "direct_match",
          epistemic_status: "OPEN_ISSUE",
          utility_score: 0.81,
          redundancy_penalty: 0.03,
          coverage_gain: 0.5,
          marginal_gain_per_token: 0.007,
          token_cost: 50,
        },
      ],
      rejected_candidates: [
        {
          turn_id: "turn_1",
          turn_ordinal: 0,
          raw_score: 0.8,
          epistemic_status: "PROPOSED",
          reason_code: "SUPERSEDED_BY_NEWER_DECISION",
          detail: "Superseded by confirmed turn turn_3",
        },
      ],
    })!;

    const messages: SessionTranscriptTurnLike[] = [
      { id: "turn_1", role: "user", content: "Propose 5% SLA penalty." },
      { id: "turn_2", role: "assistant", content: "Noted 5%." },
      {
        id: "turn_3",
        role: "user",
        content: "Confirm 8% SLA penalty; liability cap remains open.",
      },
      { id: "turn_4", role: "assistant", content: "Confirmed 8%." },
    ];

    const lineage = buildDecisionLineage({ currentAudit: audit, messages });
    expect(lineage.chains).toHaveLength(2);
    expect(lineage.chains[0].hasSupersession).toBe(true);
    expect(lineage.chains[0].latestStatus).toBe("CONFIRMED");
    expect(lineage.chains[0].nodes.map((n) => [n.turnId, n.epistemicStatus, n.citationTag])).toEqual([
      ["turn_1", "SUPERSEDED", undefined],
      ["turn_3", "CONFIRMED", "S1"],
    ]);
    expect(lineage.chains[1].hasSupersession).toBe(false);
    expect(lineage.chains[1].latestStatus).toBe("OPEN_ISSUE");
    expect(lineage.chains[1].nodes[0].citationTag).toBe("S2");
  });

  it("computes budget allocation and view weight percentages accurately", () => {
    const audit = normalizeSessionContextAudit(SAMPLE_AUDIT_RAW)!;
    const budget = computeBudgetBreakdown(audit);
    expect(budget.totalUsedTokens).toBe(390);
    expect(budget.remainingTokens).toBe(1210);
    expect(budget.overBudget).toBe(false);
    expect(Math.round(budget.totalPercent)).toBe(24);

    const views = normalizeViewWeights(audit.view_weights);
    expect(views.map((v) => v.key)).toEqual([
      "state",
      "graph",
      "hybrid",
      "hierarchy",
    ]);
    expect(Math.round(views[0].sharePercent)).toBe(34);
  });
});
