import { describe, expect, it } from "vitest";

import {
  buildDecisionLineage,
  computeBudgetBreakdown,
  extractAnswerCitationRefs,
  extractAnswerSessionCitationRefs,
  filterAnswerSessionCitations,
  normalizeSessionContextAudit,
  normalizeViewWeights,
  parseCitationBrackets,
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

  it("extracts single and compound citations from prose while ignoring code and Markdown links", () => {
    const markdown = [
      "Final architecture uses PostgreSQL + Qdrant [S1, S4] and ADR v2 [1, 2].",
      "Mixed citation [3, S5] and semicolon compound [S6; S7] plus adjacent [4][S8].",
      "Ignore Markdown link [S9](https://example.com/spec) and inline code `[S2, 9]` and fenced blocks:",
      "```json",
      '{ "example": "[S3, 10]" }',
      "```",
    ].join("\n");

    const { docNumbers, sessionRefs } = extractAnswerCitationRefs(markdown);
    expect([...docNumbers]).toEqual(["1", "2", "3", "4"]);
    expect([...sessionRefs]).toEqual(["S1", "S4", "S5", "S6", "S7", "S8"]);
    expect([...extractAnswerSessionCitationRefs(markdown)]).toEqual([
      "S1",
      "S4",
      "S5",
      "S6",
      "S7",
      "S8",
    ]);
    expect(parseCitationBrackets("See [1, S1] and [S2](https://x.test)")).toHaveLength(1);
  });

  it("resolves evidence groups, recent continuity [S{k+1}], and unloaded older turns from backend provenance", () => {
    const audit = normalizeSessionContextAudit({
      ...SAMPLE_AUDIT_RAW,
      recent_turn_ids: ["turn_4", "turn_5"],
    })!;
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
      sessionCitations: [
        {
          ref: "S1",
          primary_message_id: "turn_3",
          message_ids: ["turn_2", "turn_3"],
          turn_ordinals: [1, 2],
          epistemic_status: "CONFIRMED",
          mapped: true,
        },
        {
          ref: "S2",
          primary_message_id: "turn_5",
          message_ids: ["turn_4", "turn_5"],
          turn_ordinals: [3, 4],
          epistemic_status: "FACT",
          mapped: true,
        },
      ],
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
    expect(resolved[1].includedTurnIds).toEqual(["turn_4", "turn_5"]);
    expect(resolved[1].turns.some((t) => t.id === "turn_4")).toBe(true);

    // Shadow mode keeps citations out of the answer Reference Center while preserving Inspector data
    const shadowAudit = normalizeSessionContextAudit({ ...SAMPLE_AUDIT_RAW, mode: "shadow" })!;
    expect(filterAnswerSessionCitations(shadowAudit, resolved)).toEqual([]);
    expect(filterAnswerSessionCitations(audit, resolved)).toHaveLength(2);
  });

  it("never guesses recent continuity turns by fixed slice (PR #46 Finding 1 regression)", () => {
    const history: SessionTranscriptTurnLike[] = Array.from({ length: 8 }, (_, idx) => ({
      id: `u${idx + 1}`,
      role: idx % 2 === 0 ? ("user" as const) : ("assistant" as const),
      content: `Turn content u${idx + 1}`,
    }));
    history.push({
      id: "a_final",
      role: "assistant",
      content: "Summary citing [S1, S2].",
    });

    const audit = normalizeSessionContextAudit({
      ...SAMPLE_AUDIT_RAW,
      memory_scope: "session_history",
      selected_groups: [
        {
          citation_tag: "S1",
          primary_turn_id: "u1",
          included_turn_ids: ["u1"],
          closure_reason: "direct_match",
          epistemic_status: "CONFIRMED",
          utility_score: 0.9,
          redundancy_penalty: 0,
          coverage_gain: 0.5,
          marginal_gain_per_token: 0.02,
          token_cost: 40,
        },
      ],
      rejected_candidates: [],
    })!;

    // Backend session_history only packed u7, u8 into recent context (NOT u5..u8)
    const withBackendProvenance = resolveSessionCitations({
      content: "Summary citing [S1, S2].",
      audit,
      messages: history,
      assistantMessageId: "a_final",
      sessionCitations: [
        {
          ref: "S1",
          primary_message_id: "u1",
          message_ids: ["u1"],
          turn_ordinals: [1],
          epistemic_status: "CONFIRMED",
          mapped: true,
        },
        {
          ref: "S2",
          primary_message_id: "u8",
          message_ids: ["u7", "u8"],
          turn_ordinals: [7, 8],
          epistemic_status: "FACT",
          mapped: true,
        },
      ],
    });

    expect(withBackendProvenance).toHaveLength(2);
    expect(withBackendProvenance[1].ref).toBe("S2");
    expect(withBackendProvenance[1].includedTurnIds).toEqual(["u7", "u8"]);
    expect(withBackendProvenance[1].turns.map((t) => t.id)).toEqual(["u7", "u8"]);

    // Without backend recent provenance, UI must NOT fabricate S2 from history.slice(-4)
    const withoutBackendRecent = resolveSessionCitations({
      content: "Summary citing [S1, S2].",
      audit,
      messages: history,
      assistantMessageId: "a_final",
    });
    expect(withoutBackendRecent.map((item) => item.ref)).toEqual(["S1"]);
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
      assistantMessageId: "turn_6",
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

    // PR #46 Finding 2 regression: inspecting an earlier assistant turn (`turn_4`)
    // must NOT leak future supersession edges (`turn_3 -> turn_5`) from `turn_6`.
    const earlierLineage = buildDecisionLineage({
      currentAudit: earlierAudit,
      messages,
      assistantMessageId: "turn_4",
    });
    expect(earlierLineage.supersededCount).toBe(1);
    expect(earlierLineage.chains).toHaveLength(1);
    expect(earlierLineage.chains[0].nodes.map((n) => [n.turnId, n.epistemicStatus])).toEqual([
      ["turn_1", "SUPERSEDED"],
      ["turn_3", "CONFIRMED"],
    ]);
  });

  it("isolates first confirmed decision from later superseding turns (PR #46 Finding 2 exact repro)", () => {
    const auditTurn1 = normalizeSessionContextAudit({
      ...SAMPLE_AUDIT_RAW,
      selected_groups: [
        {
          citation_tag: "S1",
          primary_turn_id: "u_a",
          included_turn_ids: ["u_a"],
          closure_reason: "direct_match",
          epistemic_status: "CONFIRMED",
          utility_score: 0.9,
          redundancy_penalty: 0,
          coverage_gain: 0.5,
          marginal_gain_per_token: 0.02,
          token_cost: 40,
        },
      ],
      rejected_candidates: [],
    })!;

    const auditTurn2 = normalizeSessionContextAudit({
      ...SAMPLE_AUDIT_RAW,
      selected_groups: [
        {
          citation_tag: "S1",
          primary_turn_id: "u_b",
          included_turn_ids: ["u_b"],
          closure_reason: "direct_match",
          epistemic_status: "CONFIRMED",
          utility_score: 0.95,
          redundancy_penalty: 0,
          coverage_gain: 0.5,
          marginal_gain_per_token: 0.03,
          token_cost: 44,
        },
      ],
      rejected_candidates: [
        {
          turn_id: "u_a",
          turn_ordinal: 0,
          raw_score: 0.8,
          epistemic_status: "SUPERSEDED",
          reason_code: "SUPERSEDED_BY_NEWER_DECISION",
          detail: "Superseded by confirmed turn u_b",
        },
      ],
    })!;

    const messages: SessionTranscriptTurnLike[] = [
      { id: "u_a", role: "user", content: "Confirm option A." },
      {
        id: "a_1",
        role: "assistant",
        content: "Confirmed option A [S1].",
        steps: [{ kind: "session_context", step: 0, audit: auditTurn1 }],
      },
      { id: "u_b", role: "user", content: "Replace option A with option B." },
      {
        id: "a_2",
        role: "assistant",
        content: "Replaced A with option B [S1].",
        steps: [{ kind: "session_context", step: 0, audit: auditTurn2 }],
      },
    ];

    const lineageAtTurn1 = buildDecisionLineage({
      currentAudit: auditTurn1,
      messages,
      assistantMessageId: "a_1",
    });
    expect(lineageAtTurn1.supersededCount).toBe(0);
    expect(lineageAtTurn1.chains).toHaveLength(1);
    expect(lineageAtTurn1.chains[0].nodes.map((n) => [n.turnId, n.epistemicStatus])).toEqual([
      ["u_a", "CONFIRMED"],
    ]);

    const lineageAtTurn2 = buildDecisionLineage({
      currentAudit: auditTurn2,
      messages,
      assistantMessageId: "a_2",
    });
    expect(lineageAtTurn2.supersededCount).toBe(1);
    expect(lineageAtTurn2.chains).toHaveLength(1);
    expect(lineageAtTurn2.chains[0].nodes.map((n) => [n.turnId, n.epistemicStatus])).toEqual([
      ["u_a", "SUPERSEDED"],
      ["u_b", "CONFIRMED"],
    ]);
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
