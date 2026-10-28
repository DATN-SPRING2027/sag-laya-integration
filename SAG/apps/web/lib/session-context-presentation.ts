import type {
  BackendSessionCitationTrace,
  EpistemicStatus,
  MessageStep,
  RejectedCandidateTrace,
  SelectedEvidenceGroupTrace,
  SessionCitation,
  SessionCitationTurnPreview,
  SessionContextAudit,
  SessionContextMode,
} from "./types";

const KNOWN_EPISTEMIC_STATUSES: ReadonlySet<EpistemicStatus> = new Set([
  "FACT",
  "PROPOSED",
  "CONSIDERING",
  "CONFIRMED",
  "SUPERSEDED",
  "NEGATED",
  "OPEN_ISSUE",
]);

const DECISION_RELEVANT_STATUSES: ReadonlySet<EpistemicStatus> = new Set([
  "PROPOSED",
  "CONSIDERING",
  "CONFIRMED",
  "SUPERSEDED",
  "NEGATED",
  "OPEN_ISSUE",
]);

const KNOWN_MODES: ReadonlySet<SessionContextMode> = new Set([
  "legacy",
  "shadow",
  "on",
]);

export type EpistemicTone =
  | "confirmed"
  | "considering"
  | "superseded"
  | "issue"
  | "fact";

export interface SessionTranscriptTurnLike {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  createdAt?: string;
  steps?: readonly MessageStep[];
}

export interface ParsedSessionContextStep {
  step: MessageStep;
  audit: SessionContextAudit;
  sessionCitations: BackendSessionCitationTrace[];
  transitiveSources: Array<{ id: string; name?: string }>;
}

export type ParsedCitationToken =
  | { kind: "doc"; number: string }
  | { kind: "session"; ref: string };

export interface ParsedCitationBracket {
  start: number;
  end: number;
  raw: string;
  tokens: ParsedCitationToken[];
}

export interface DecisionLineageNode {
  turnId: string;
  ordinal?: number;
  role?: "user" | "assistant" | "system";
  excerpt: string;
  createdAt?: string;
  loaded: boolean;
  epistemicStatus: EpistemicStatus;
  citationTag?: string;
  selectedInCurrentTurn: boolean;
  supersededByTurnId?: string;
}

export interface DecisionLineageChain {
  id: string;
  nodes: DecisionLineageNode[];
  latestStatus: EpistemicStatus;
  hasSupersession: boolean;
  activeInCurrentTurn: boolean;
}

export interface DecisionLineageModel {
  chains: DecisionLineageChain[];
  totalTrackedDecisions: number;
  supersededCount: number;
}

export interface ViewWeightEntry {
  key: "hybrid" | "graph" | "hierarchy" | "state" | "short_verbatim" | "legacy" | string;
  weight: number;
  sharePercent: number;
}

export interface BudgetBreakdown {
  budgetTokens: number;
  usedEvidenceTokens: number;
  usedRecentTokens: number;
  totalUsedTokens: number;
  remainingTokens: number;
  evidencePercent: number;
  recentPercent: number;
  totalPercent: number;
  overBudget: boolean;
}

function asObject(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function safeFiniteNumber(value: unknown, fallback = 0): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function safeNonNegativeInt(value: unknown, fallback = 0): number {
  return Math.max(0, Math.round(safeFiniteNumber(value, fallback)));
}

function safeString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export function normalizeEpistemicStatus(value: unknown): EpistemicStatus {
  if (typeof value === "string") {
    const upper = value.trim().toUpperCase() as EpistemicStatus;
    if (KNOWN_EPISTEMIC_STATUSES.has(upper)) return upper;
  }
  return "FACT";
}

export function normalizeSessionMode(
  value: unknown,
  fallback: SessionContextMode = "legacy",
): SessionContextMode {
  if (typeof value === "string") {
    const lower = value.trim().toLowerCase() as SessionContextMode;
    if (KNOWN_MODES.has(lower)) return lower;
  }
  return fallback;
}

function normalizeSelectedGroup(
  raw: unknown,
  fallbackIndex: number,
): SelectedEvidenceGroupTrace | null {
  const item = asObject(raw);
  if (!item) return null;
  const rawTag = safeString(item.citation_tag).trim().toUpperCase();
  const citationTag = /^S[1-9]\d*$/.test(rawTag) ? rawTag : `S${fallbackIndex}`;
  const primaryTurnId = safeString(item.primary_turn_id).trim();
  const includedTurnIds = Array.isArray(item.included_turn_ids)
    ? item.included_turn_ids
        .map((entry) => safeString(entry).trim())
        .filter((entry) => entry.length > 0)
    : primaryTurnId
      ? [primaryTurnId]
      : [];
  const effectivePrimary = primaryTurnId || includedTurnIds[0] || "";
  if (!effectivePrimary && includedTurnIds.length === 0) return null;

  return {
    citation_tag: citationTag,
    primary_turn_id: effectivePrimary,
    included_turn_ids: includedTurnIds.length > 0 ? includedTurnIds : [effectivePrimary],
    closure_reason: safeString(item.closure_reason, "direct_match").trim() || "direct_match",
    epistemic_status: normalizeEpistemicStatus(item.epistemic_status),
    utility_score: Math.max(0, safeFiniteNumber(item.utility_score, 0)),
    redundancy_penalty: Math.max(0, safeFiniteNumber(item.redundancy_penalty, 0)),
    coverage_gain: Math.max(0, safeFiniteNumber(item.coverage_gain, 0)),
    marginal_gain_per_token: Math.max(0, safeFiniteNumber(item.marginal_gain_per_token, 0)),
    token_cost: safeNonNegativeInt(item.token_cost, 0),
  };
}

function normalizeRejectedCandidate(raw: unknown): RejectedCandidateTrace | null {
  const item = asObject(raw);
  if (!item) return null;
  const turnId = safeString(item.turn_id).trim();
  if (!turnId) return null;
  return {
    turn_id: turnId,
    turn_ordinal: safeNonNegativeInt(item.turn_ordinal, 0),
    raw_score: Math.max(0, safeFiniteNumber(item.raw_score, 0)),
    epistemic_status: normalizeEpistemicStatus(item.epistemic_status),
    reason_code: safeString(item.reason_code, "LOW_MARGINAL_UTILITY").trim() || "LOW_MARGINAL_UTILITY",
    detail: safeString(item.detail).trim(),
  };
}

export function normalizeBackendSessionCitation(
  raw: unknown,
): BackendSessionCitationTrace | null {
  const item = asObject(raw);
  if (!item) return null;

  const rawRef = safeString(item.ref).trim().toUpperCase();
  if (!/^S[1-9]\d*$/.test(rawRef)) return null;
  const canonicalRef = `S${Number.parseInt(rawRef.slice(1), 10)}`;

  const rawIds = Array.isArray(item.message_ids)
    ? item.message_ids
    : Array.isArray(item.included_turn_ids)
      ? item.included_turn_ids
      : Array.isArray(item.includedTurnIds)
        ? item.includedTurnIds
        : Array.isArray(item.turn_ids)
          ? item.turn_ids
          : [];
  const messageIds = rawIds
    .map((entry) => safeString(entry).trim())
    .filter((entry) => entry.length > 0);
  const rawPrimary = safeString(
    item.primary_message_id ?? item.primary_turn_id ?? item.primaryTurnId,
  ).trim();
  if (messageIds.length === 0 && rawPrimary) {
    messageIds.push(rawPrimary);
  }
  if (messageIds.length === 0) return null;

  const primaryMessageId =
    rawPrimary || messageIds[messageIds.length - 1] || null;

  const rawOrdinals = Array.isArray(item.turn_ordinals)
    ? item.turn_ordinals
    : Array.isArray(item.turnOrdinals)
      ? item.turnOrdinals
      : undefined;
  const turnOrdinals = rawOrdinals
    ? rawOrdinals
        .map((ord) => safeNonNegativeInt(ord, 0))
        .filter((ord) => ord > 0)
    : undefined;

  const rawCitedSources = Array.isArray(item.cited_source_ids)
    ? item.cited_source_ids
        .map((sid) => safeString(sid).trim())
        .filter((sid) => sid.length > 0)
    : undefined;

  return {
    ...(typeof item.kind === "string" && item.kind.trim()
      ? { kind: item.kind.trim() }
      : {}),
    ref: canonicalRef,
    primary_message_id: primaryMessageId,
    message_ids: messageIds,
    ...(turnOrdinals && turnOrdinals.length > 0
      ? { turn_ordinals: turnOrdinals }
      : {}),
    epistemic_status: normalizeEpistemicStatus(
      item.epistemic_status ?? item.epistemicStatus ?? item.status,
    ),
    ...(typeof item.sha256 === "string" && item.sha256.trim()
      ? { sha256: item.sha256.trim() }
      : {}),
    ...(rawCitedSources && rawCitedSources.length > 0
      ? { cited_source_ids: rawCitedSources }
      : {}),
    ...(typeof item.mapped === "boolean" ? { mapped: item.mapped } : {}),
    ...(typeof item.claim_level === "string" && item.claim_level.trim()
      ? { claim_level: item.claim_level.trim() }
      : {}),
  };
}

export function normalizeSessionContextAudit(
  raw: unknown,
  stepModeFallback?: unknown,
  stepMsFallback?: unknown,
): SessionContextAudit | null {
  const data = asObject(raw);
  if (!data) return null;

  const rawWeights = asObject(data.view_weights);
  const viewWeights: Record<string, number> = {};
  if (rawWeights) {
    for (const [key, weight] of Object.entries(rawWeights)) {
      const numeric = safeFiniteNumber(weight, NaN);
      if (key.trim() && Number.isFinite(numeric) && numeric >= 0) {
        viewWeights[key.trim()] = numeric;
      }
    }
  }

  const selectedGroups = Array.isArray(data.selected_groups)
    ? data.selected_groups
        .map((group, index) => normalizeSelectedGroup(group, index + 1))
        .filter((group): group is SelectedEvidenceGroupTrace => group !== null)
    : [];

  const recentTurnIds = Array.isArray(data.recent_turn_ids)
    ? data.recent_turn_ids
        .map((entry) => safeString(entry).trim())
        .filter((entry) => entry.length > 0)
    : [];

  const auditSessionCitations = Array.isArray(data.session_citations)
    ? data.session_citations
        .map(normalizeBackendSessionCitation)
        .filter((entry): entry is BackendSessionCitationTrace => entry !== null)
    : undefined;

  const rejectedCandidates = Array.isArray(data.rejected_candidates)
    ? data.rejected_candidates
        .map(normalizeRejectedCandidate)
        .filter((candidate): candidate is RejectedCandidateTrace => candidate !== null)
    : [];

  const mode = normalizeSessionMode(
    data.mode,
    normalizeSessionMode(stepModeFallback, "legacy"),
  );
  const usedEvidenceTokens = safeNonNegativeInt(data.used_evidence_tokens, 0);
  const usedRecentTokens = safeNonNegativeInt(data.used_recent_tokens, 0);
  const totalOutputTokens = safeNonNegativeInt(
    data.total_output_history_tokens,
    usedEvidenceTokens + usedRecentTokens,
  );

  return {
    mode,
    ablation: safeString(data.ablation, "V5_ecce_full").trim() || "V5_ecce_full",
    memory_scope: safeString(data.memory_scope, "session_history").trim() || "session_history",
    temporal_mode: safeString(data.temporal_mode, "current_effective").trim() || "current_effective",
    router_model: safeString(data.router_model, "deterministic").trim() || "deterministic",
    router_fallback_used: Boolean(data.router_fallback_used),
    total_indexed_turns: safeNonNegativeInt(data.total_indexed_turns, 0),
    episode_count: safeNonNegativeInt(data.episode_count, 0),
    active_decisions_count: safeNonNegativeInt(data.active_decisions_count, 0),
    budget_tokens: safeNonNegativeInt(data.budget_tokens, 0),
    used_evidence_tokens: usedEvidenceTokens,
    used_recent_tokens: usedRecentTokens,
    total_output_history_tokens: totalOutputTokens,
    llm_memory_calls: safeNonNegativeInt(data.llm_memory_calls, 0),
    partial_coverage: Boolean(data.partial_coverage),
    view_weights: viewWeights,
    selected_groups: selectedGroups,
    recent_turn_ids: recentTurnIds,
    ...(auditSessionCitations && auditSessionCitations.length > 0
      ? { session_citations: auditSessionCitations }
      : {}),
    rejected_candidates: rejectedCandidates,
    latency_ms: Math.max(0, safeFiniteNumber(data.latency_ms, safeFiniteNumber(stepMsFallback, 0))),
    persistence_status: safeString(data.persistence_status, "memory_only").trim() || "memory_only",
    persisted_units_written: safeNonNegativeInt(data.persisted_units_written, 0),
    qdrant_points_upserted: safeNonNegativeInt(data.qdrant_points_upserted, 0),
  };
}

export function parseSessionContextStep(
  steps?: readonly MessageStep[] | null,
): ParsedSessionContextStep | null {
  if (!steps || steps.length === 0) return null;
  const step = steps.find(
    (candidate) =>
      candidate.kind === "session_context" || candidate.name === "session_context",
  );
  if (!step) return null;
  const audit = normalizeSessionContextAudit(step.audit, step.mode, step.ms);
  if (!audit) return null;

  const rawStepCitations = Array.isArray(step.session_citations)
    ? step.session_citations
    : audit.session_citations ?? [];
  const sessionCitations = rawStepCitations
    .map(normalizeBackendSessionCitation)
    .filter((entry): entry is BackendSessionCitationTrace => entry !== null);

  const transitiveSources = (step.details?.sources ?? [])
    .map((source) => ({
      id: safeString(source.id).trim(),
      ...(typeof source.name === "string" && source.name.trim()
        ? { name: source.name.trim() }
        : {}),
    }))
    .filter((source) => source.id.length > 0);

  return {
    step,
    audit,
    sessionCitations,
    transitiveSources,
  };
}

export function cleanTurnExcerpt(text: string, maxChars = 220): string {
  const normalized = text
    .replace(/```[\s\S]*?```/g, " [code] ")
    .replace(/`([^`\n]+)`/g, "$1")
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/\*\*([^*\n]+)\*\*/g, "$1")
    .replace(/__([^_\n]+)__/g, "$1")
    .replace(/^[#>*\-\s]+/gm, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!normalized) return "";
  return normalized.length > maxChars
    ? `${normalized.slice(0, maxChars - 1).trimEnd()}…`
    : normalized;
}

const CITATION_BRACKET_RE =
  /\[(\s*(?:[1-9]\d*|S[1-9]\d*)(?:\s*[,;、]\s*(?:[1-9]\d*|S[1-9]\d*))*)\s*\](?!\()/g;

/**
 * Shared parser for single (`[1]`, `[S1]`) and compound (`[1, 2]`, `[S1, S2]`, `[1, S1]`)
 * bracketed citation groups in prose text. Ignores Markdown inline links `[1](https://...)`.
 */
export function parseCitationBrackets(text: string): ParsedCitationBracket[] {
  if (!text) return [];
  const results: ParsedCitationBracket[] = [];
  const re = new RegExp(CITATION_BRACKET_RE.source, "g");
  let match: RegExpExecArray | null;
  while ((match = re.exec(text))) {
    const inner = match[1] ?? "";
    const parts = inner
      .split(/[,;、]/)
      .map((part) => part.trim())
      .filter((part) => part.length > 0);
    if (parts.length === 0) continue;

    const tokens: ParsedCitationToken[] = [];
    let allMalformedFree = true;
    for (const part of parts) {
      if (/^S[1-9]\d*$/.test(part)) {
        tokens.push({ kind: "session", ref: `S${Number.parseInt(part.slice(1), 10)}` });
      } else if (/^[1-9]\d*$/.test(part)) {
        tokens.push({ kind: "doc", number: String(Number.parseInt(part, 10)) });
      } else {
        allMalformedFree = false;
        break;
      }
    }
    if (!allMalformedFree || tokens.length === 0) continue;

    results.push({
      start: match.index,
      end: match.index + match[0].length,
      raw: match[0],
      tokens,
    });
  }
  return results;
}

function stripMarkdownCodeForCitationScan(content: string): string {
  if (!content) return "";
  let codeFence: string | null = null;
  let inlineCodeTicks = 0;
  let proseBuffer = "";

  for (let index = 0; index < content.length;) {
    const lineStart = index === 0 || content[index - 1] === "\n";
    if (lineStart && inlineCodeTicks === 0) {
      const fenceMatch = /^( {0,3})(`{3,}|~{3,})/.exec(content.slice(index));
      if (fenceMatch) {
        const marker = fenceMatch[2];
        const newlineIndex = content.indexOf("\n", index);
        const lineEnd = newlineIndex === -1 ? content.length : newlineIndex + 1;
        const suffix = content.slice(index + fenceMatch[0].length, lineEnd).trim();
        if (!codeFence) codeFence = marker;
        else if (
          codeFence[0] === marker[0]
          && marker.length >= codeFence.length
          && suffix === ""
        ) {
          codeFence = null;
        }
        proseBuffer += "\n";
        index = lineEnd;
        continue;
      }
    }

    if (!codeFence && content[index] === "`") {
      let runLength = 1;
      while (content[index + runLength] === "`") runLength++;
      if (inlineCodeTicks === 0) inlineCodeTicks = runLength;
      else if (inlineCodeTicks === runLength) inlineCodeTicks = 0;
      proseBuffer += " ";
      index += runLength;
      continue;
    }

    if (!codeFence && inlineCodeTicks === 0) {
      proseBuffer += content[index];
    } else if (content[index] === "\n") {
      proseBuffer += "\n";
    }
    index++;
  }

  return proseBuffer;
}

/**
 * Extract both document (`1`, `2`) and session (`S1`, `S2`) citation markers referenced
 * in Markdown prose (single or compound `[1, 2]`, `[S1, S2]`, `[1, S1]`), ignoring
 * fenced code blocks, inline code, and Markdown links.
 */
export function extractAnswerCitationRefs(content: string): {
  docNumbers: Set<string>;
  sessionRefs: Set<string>;
} {
  const docNumbers = new Set<string>();
  const sessionRefs = new Set<string>();
  const prose = stripMarkdownCodeForCitationScan(content);
  for (const bracket of parseCitationBrackets(prose)) {
    for (const token of bracket.tokens) {
      if (token.kind === "doc") docNumbers.add(token.number);
      else sessionRefs.add(token.ref);
    }
  }
  return { docNumbers, sessionRefs };
}

/**
 * Extract `[S1]`, `[S2]` markers (including compound `[S1, S2]`, `[1, S1]`) referenced in
 * Markdown prose outside code blocks / inline code.
 */
export function extractAnswerSessionCitationRefs(content: string): Set<string> {
  return extractAnswerCitationRefs(content).sessionRefs;
}

function buildTurnLookup(messages: readonly SessionTranscriptTurnLike[]) {
  const byId = new Map<string, SessionTranscriptTurnLike>();
  const ordinalById = new Map<string, number>();
  let visibleOrdinal = 0;
  for (const message of messages) {
    byId.set(message.id, message);
    if (message.role === "user" || message.role === "assistant") {
      visibleOrdinal += 1;
      ordinalById.set(message.id, visibleOrdinal);
    }
  }
  return { byId, ordinalById };
}

function resolveTurnPreview(
  turnId: string,
  byId: ReadonlyMap<string, SessionTranscriptTurnLike>,
  ordinalById: ReadonlyMap<string, number>,
  fallbackOrdinal?: number,
): SessionCitationTurnPreview {
  const found = byId.get(turnId);
  if (found) {
    return {
      id: found.id,
      role: found.role,
      excerpt: cleanTurnExcerpt(found.content),
      createdAt: found.createdAt,
      ordinal: ordinalById.get(found.id) ?? fallbackOrdinal,
      loaded: true,
    };
  }
  return {
    id: turnId,
    role: "user",
    excerpt: "",
    ordinal: fallbackOrdinal,
    loaded: false,
  };
}

/**
 * Resolve `[S1]..[Sk]` session citations for an assistant message strictly from
 * backend-supplied provenance (`audit.selected_groups`, `sessionCitations`, or
 * `audit.recent_turn_ids`). Client `messages` are used exclusively to enrich turn
 * previews (`role`, `excerpt`, `createdAt`, `loaded`) — never to guess turn IDs.
 */
export function resolveSessionCitations({
  content,
  audit,
  messages,
  sessionCitations: rawBackendCitations,
}: {
  content: string;
  audit: SessionContextAudit | null;
  messages: readonly SessionTranscriptTurnLike[];
  assistantMessageId?: string;
  sessionCitations?: readonly unknown[];
}): SessionCitation[] {
  if (!audit || audit.mode === "legacy") return [];

  const { byId, ordinalById } = buildTurnLookup(messages);
  const answerRefs = extractAnswerSessionCitationRefs(content);

  const backendByRef = new Map<string, BackendSessionCitationTrace>();
  const rawPool =
    rawBackendCitations !== undefined
      ? rawBackendCitations
      : audit.session_citations ?? [];
  for (const entry of rawPool) {
    const normalized = normalizeBackendSessionCitation(entry);
    if (normalized && !backendByRef.has(normalized.ref)) {
      backendByRef.set(normalized.ref, normalized);
    }
  }

  const citations: SessionCitation[] = [];
  const seenRefs = new Set<string>();

  for (const [idx, group] of audit.selected_groups.entries()) {
    const ref = group.citation_tag;
    if (seenRefs.has(ref)) continue;
    seenRefs.add(ref);
    const parsedNum = Number.parseInt(ref.slice(1), 10);
    const index = Number.isInteger(parsedNum) && parsedNum > 0 ? parsedNum : idx + 1;
    const backendCit = backendByRef.get(ref);
    const turnIds =
      backendCit && backendCit.message_ids.length > 0
        ? [...backendCit.message_ids]
        : [...group.included_turn_ids];
    const turns = turnIds.map((turnId, turnIdx) =>
      resolveTurnPreview(
        turnId,
        byId,
        ordinalById,
        backendCit?.turn_ordinals?.[turnIdx],
      ),
    );
    citations.push({
      ref,
      index,
      kind: "evidence_group",
      primaryTurnId:
        backendCit?.primary_message_id
        || group.primary_turn_id
        || turnIds[0]
        || null,
      includedTurnIds: turnIds,
      epistemicStatus: group.epistemic_status,
      closureReason: group.closure_reason,
      utilityScore: group.utility_score,
      redundancyPenalty: group.redundancy_penalty,
      coverageGain: group.coverage_gain,
      marginalGainPerToken: group.marginal_gain_per_token,
      tokenCost: group.token_cost,
      turns,
      mappedInAnswer: answerRefs.has(ref) || Boolean(backendCit?.mapped),
    });
  }

  if (audit.mode === "on") {
    // 1. Consume explicit backend session citations outside `selected_groups`
    //    (`short_verbatim` S1 or `recent_continuity` S{k+1}) with exact backend turn IDs.
    for (const backendCit of backendByRef.values()) {
      if (seenRefs.has(backendCit.ref) || backendCit.message_ids.length === 0) {
        continue;
      }
      seenRefs.add(backendCit.ref);
      const parsedNum = Number.parseInt(backendCit.ref.slice(1), 10);
      const index =
        Number.isInteger(parsedNum) && parsedNum > 0
          ? parsedNum
          : citations.length + 1;
      const isShortVerbatim =
        audit.selected_groups.length === 0
        && (audit.view_weights.short_verbatim === 1 || audit.used_evidence_tokens === 0)
        && backendCit.ref === "S1";
      const kind: SessionCitation["kind"] = isShortVerbatim
        ? "short_verbatim"
        : "recent_continuity";
      const closureReason = isShortVerbatim
        ? "short_session_verbatim"
        : "recent_continuity";
      const tokenCost = isShortVerbatim
        ? audit.total_output_history_tokens
        : audit.used_recent_tokens;
      const turns = backendCit.message_ids.map((turnId, turnIdx) =>
        resolveTurnPreview(
          turnId,
          byId,
          ordinalById,
          backendCit.turn_ordinals?.[turnIdx],
        ),
      );
      citations.push({
        ref: backendCit.ref,
        index,
        kind,
        primaryTurnId:
          backendCit.primary_message_id
          || backendCit.message_ids[backendCit.message_ids.length - 1]
          || null,
        includedTurnIds: [...backendCit.message_ids],
        epistemicStatus: backendCit.epistemic_status,
        closureReason,
        tokenCost,
        turns,
        mappedInAnswer: answerRefs.has(backendCit.ref) || Boolean(backendCit.mapped),
      });
    }

    // 2. Fallback to exact `audit.recent_turn_ids` if `session_citations` was not passed
    //    separately. Never slice or guess turn IDs from client `messages`.
    const backendRecentIds = audit.recent_turn_ids ?? [];
    if (backendRecentIds.length > 0) {
      const isShortVerbatim =
        audit.selected_groups.length === 0 && audit.view_weights.short_verbatim === 1;
      if (isShortVerbatim && !seenRefs.has("S1")) {
        seenRefs.add("S1");
        const primaryId = backendRecentIds[backendRecentIds.length - 1] ?? null;
        citations.push({
          ref: "S1",
          index: 1,
          kind: "short_verbatim",
          primaryTurnId: primaryId,
          includedTurnIds: [...backendRecentIds],
          epistemicStatus: "FACT",
          closureReason: "short_session_verbatim",
          tokenCost: audit.total_output_history_tokens,
          turns: backendRecentIds.map((turnId) =>
            resolveTurnPreview(turnId, byId, ordinalById),
          ),
          mappedInAnswer: answerRefs.has("S1"),
        });
      } else if (!isShortVerbatim && audit.used_recent_tokens > 0) {
        const recentIndex = audit.selected_groups.length + 1;
        const recentRef = `S${recentIndex}`;
        if (!seenRefs.has(recentRef) && answerRefs.has(recentRef)) {
          seenRefs.add(recentRef);
          const primaryId = backendRecentIds[backendRecentIds.length - 1] ?? null;
          citations.push({
            ref: recentRef,
            index: recentIndex,
            kind: "recent_continuity",
            primaryTurnId: primaryId,
            includedTurnIds: [...backendRecentIds],
            epistemicStatus: "FACT",
            closureReason: "recent_continuity",
            tokenCost: audit.used_recent_tokens,
            turns: backendRecentIds.map((turnId) =>
              resolveTurnPreview(turnId, byId, ordinalById),
            ),
            mappedInAnswer: true,
          });
        }
      }
    }
  }

  return citations.sort((left, right) => left.index - right.index);
}

/**
 * Filter session citations suitable for the answer's Reference Center (`CitationBlock`).
 * Shadow runs do not inject session citations into the answer prompt, so they stay in
 * the Context Inspector only rather than appearing as answer references.
 */
export function filterAnswerSessionCitations(
  audit: SessionContextAudit | null,
  sessionCitations: readonly SessionCitation[],
): SessionCitation[] {
  if (!audit || audit.mode !== "on") return [];
  return sessionCitations.filter(
    (item) => item.kind === "evidence_group" || item.mappedInAnswer,
  );
}

const SUPERSEDED_BY_DETAIL_RE = /Superseded by confirmed turn ([A-Za-z0-9_-]+)/i;

export function extractSupersededByTurnId(detail: string): string | null {
  const match = SUPERSEDED_BY_DETAIL_RE.exec(detail);
  return match?.[1] ? match[1] : null;
}

const EPISTEMIC_LINEAGE_RANK: Record<EpistemicStatus, number> = {
  SUPERSEDED: 6,
  NEGATED: 5,
  CONFIRMED: 4,
  PROPOSED: 3,
  CONSIDERING: 2,
  OPEN_ISSUE: 1,
  FACT: 0,
};

/**
 * Build chronological Decision Superseding Lineage chains strictly as of the inspected
 * assistant turn (`assistantMessageId`), combining earlier assistant audits up to that
 * turn with `currentAudit` and excluding any future turns in the thread.
 */
export function buildDecisionLineage({
  currentAudit,
  messages,
  assistantMessageId,
}: {
  currentAudit: SessionContextAudit | null;
  messages: readonly SessionTranscriptTurnLike[];
  assistantMessageId?: string;
}): DecisionLineageModel {
  if (!currentAudit) {
    return { chains: [], totalTrackedDecisions: 0, supersededCount: 0 };
  }

  let lookupMessages: readonly SessionTranscriptTurnLike[] = messages;
  let priorMessages: readonly SessionTranscriptTurnLike[] = messages;
  if (typeof assistantMessageId === "string" && assistantMessageId.trim().length > 0) {
    const targetIndex = messages.findIndex((msg) => msg.id === assistantMessageId);
    if (targetIndex >= 0) {
      lookupMessages = messages.slice(0, targetIndex + 1);
      priorMessages = messages.slice(0, targetIndex);
    } else {
      priorMessages = [];
    }
  }

  const { byId, ordinalById } = buildTurnLookup(lookupMessages);

  const statusByTurn = new Map<string, EpistemicStatus>();
  const fallbackOrdinalByTurn = new Map<string, number>();
  const supersededNextByTurn = new Map<string, string>();
  const currentCitationByTurn = new Map<string, string>();
  const currentCitationRankByTurn = new Map<string, number>();
  const currentCitationByTurnAndStatus = new Map<string, string>();
  const lineageClosureCitationByTurn = new Map<string, string>();
  const currentSelectedTurns = new Set<string>();

  const setTurnStatus = (turnId: string, nextStatus: EpistemicStatus) => {
    if (!turnId) return;
    const prev = statusByTurn.get(turnId);
    if (!prev || EPISTEMIC_LINEAGE_RANK[nextStatus] >= EPISTEMIC_LINEAGE_RANK[prev]) {
      statusByTurn.set(turnId, nextStatus);
    }
  };

  const setCurrentTurnCitation = (
    turnId: string,
    citationTag: string,
    status: EpistemicStatus,
  ) => {
    if (!turnId || !citationTag) return;
    currentCitationByTurnAndStatus.set(`${turnId}:${status}`, citationTag);
    const rank = EPISTEMIC_LINEAGE_RANK[status];
    const prevRank = currentCitationRankByTurn.get(turnId);
    if (prevRank === undefined || rank > prevRank) {
      currentCitationByTurn.set(turnId, citationTag);
      currentCitationRankByTurn.set(turnId, rank);
    }
  };

  const recordAudit = (audit: SessionContextAudit, isCurrent: boolean) => {
    for (const group of audit.selected_groups) {
      if (isCurrent) {
        setCurrentTurnCitation(
          group.primary_turn_id,
          group.citation_tag,
          group.epistemic_status,
        );
        for (const id of group.included_turn_ids) {
          currentSelectedTurns.add(id);
          if (!currentCitationByTurn.has(id)) {
            setCurrentTurnCitation(id, group.citation_tag, group.epistemic_status);
          }
          if (group.closure_reason === "decision_lineage_closure") {
            lineageClosureCitationByTurn.set(id, group.citation_tag);
          }
        }
      }

      if (
        group.closure_reason === "decision_lineage_closure"
        && group.included_turn_ids.length > 1
      ) {
        const ids = group.included_turn_ids;
        const primaryIdx = group.primary_turn_id
          ? ids.indexOf(group.primary_turn_id)
          : -1;
        const headIdx = primaryIdx >= 0 ? primaryIdx : ids.length - 1;
        for (let idx = 0; idx < headIdx; idx++) {
          const ancestorId = ids[idx];
          const nextId = ids[idx + 1];
          if (ancestorId && nextId && ancestorId !== nextId) {
            supersededNextByTurn.set(ancestorId, nextId);
            setTurnStatus(ancestorId, "SUPERSEDED");
          }
        }
        const headId = ids[headIdx];
        if (headId) {
          if (isCurrent && group.epistemic_status === "CONFIRMED") {
            supersededNextByTurn.delete(headId);
            statusByTurn.set(headId, "CONFIRMED");
          } else {
            setTurnStatus(headId, group.epistemic_status);
          }
        }
      } else if (
        DECISION_RELEVANT_STATUSES.has(group.epistemic_status)
        && group.primary_turn_id
      ) {
        if (isCurrent && group.epistemic_status === "CONFIRMED") {
          supersededNextByTurn.delete(group.primary_turn_id);
          statusByTurn.set(group.primary_turn_id, "CONFIRMED");
        } else {
          setTurnStatus(group.primary_turn_id, group.epistemic_status);
        }
      }
    }

    for (const rejected of audit.rejected_candidates) {
      fallbackOrdinalByTurn.set(rejected.turn_id, rejected.turn_ordinal + 1);
      if (DECISION_RELEVANT_STATUSES.has(rejected.epistemic_status)) {
        setTurnStatus(rejected.turn_id, rejected.epistemic_status);
      }
      if (rejected.reason_code === "SUPERSEDED_BY_NEWER_DECISION") {
        setTurnStatus(rejected.turn_id, "SUPERSEDED");
        const newerTurnId = extractSupersededByTurnId(rejected.detail);
        if (newerTurnId && newerTurnId !== rejected.turn_id) {
          supersededNextByTurn.set(rejected.turn_id, newerTurnId);
          setTurnStatus(newerTurnId, "CONFIRMED");
        }
      }
    }
  };

  // Scan strictly prior assistant audits up to the inspected turn, then apply currentAudit.
  for (const message of priorMessages) {
    if (message.role !== "assistant" || !message.steps?.length) continue;
    const parsed = parseSessionContextStep(message.steps);
    if (parsed && parsed.audit !== currentAudit) {
      recordAudit(parsed.audit, false);
    }
  }
  recordAudit(currentAudit, true);

  // Build undirected adjacency across supersession edges to group connected chains,
  // then sort each component chronologically (by ordinal, then topological supersession).
  const trackedIds = new Set<string>([
    ...statusByTurn.keys(),
    ...supersededNextByTurn.keys(),
    ...supersededNextByTurn.values(),
  ]);

  if (trackedIds.size === 0) {
    return { chains: [], totalTrackedDecisions: 0, supersededCount: 0 };
  }

  const neighbors = new Map<string, Set<string>>();
  for (const id of trackedIds) neighbors.set(id, new Set());
  for (const [older, newer] of supersededNextByTurn.entries()) {
    neighbors.get(older)?.add(newer);
    neighbors.get(newer)?.add(older);
  }

  const visited = new Set<string>();
  const chains: DecisionLineageChain[] = [];

  const getOrdinal = (id: string): number | undefined =>
    ordinalById.get(id) ?? fallbackOrdinalByTurn.get(id);

  for (const seedId of trackedIds) {
    if (visited.has(seedId)) continue;
    const component: string[] = [];
    const queue: string[] = [seedId];
    visited.add(seedId);
    while (queue.length > 0) {
      const cur = queue.shift()!;
      component.push(cur);
      for (const nxt of neighbors.get(cur) ?? []) {
        if (!visited.has(nxt)) {
          visited.add(nxt);
          queue.push(nxt);
        }
      }
    }

    // Topological + ordinal sort so older superseded turns always precede newer confirmed turns
    component.sort((left, right) => {
      if (supersededNextByTurn.get(left) === right) return -1;
      if (supersededNextByTurn.get(right) === left) return 1;
      const leftOrd = getOrdinal(left) ?? Number.MAX_SAFE_INTEGER;
      const rightOrd = getOrdinal(right) ?? Number.MAX_SAFE_INTEGER;
      if (leftOrd !== rightOrd) return leftOrd - rightOrd;
      return left.localeCompare(right);
    });

    const nodes: DecisionLineageNode[] = component.map((turnId) => {
      const preview = resolveTurnPreview(
        turnId,
        byId,
        ordinalById,
        fallbackOrdinalByTurn.get(turnId),
      );
      const supersededBy = supersededNextByTurn.get(turnId);
      const status = supersededBy
        ? "SUPERSEDED"
        : statusByTurn.get(turnId) ?? "CONFIRMED";
      const nodeCitationTag =
        status === "SUPERSEDED"
          ? lineageClosureCitationByTurn.get(turnId)
            ?? currentCitationByTurnAndStatus.get(`${turnId}:SUPERSEDED`)
          : currentCitationByTurnAndStatus.get(`${turnId}:${status}`)
            ?? currentCitationByTurn.get(turnId);
      return {
        turnId,
        ordinal: preview.ordinal,
        role: preview.loaded ? preview.role : undefined,
        excerpt: preview.excerpt,
        createdAt: preview.createdAt,
        loaded: preview.loaded,
        epistemicStatus: status,
        citationTag: nodeCitationTag,
        selectedInCurrentTurn: Boolean(nodeCitationTag) || (status !== "SUPERSEDED" && currentSelectedTurns.has(turnId)),
        supersededByTurnId: supersededBy,
      };
    });

    const tail = nodes[nodes.length - 1];
    chains.push({
      id: nodes.map((node) => node.turnId).join("->"),
      nodes,
      latestStatus: tail?.epistemicStatus ?? "FACT",
      hasSupersession: nodes.some((node) => Boolean(node.supersededByTurnId)),
      activeInCurrentTurn: nodes.some((node) => node.selectedInCurrentTurn),
    });
  }

  // Preserve distinct decision-relevant facets on the same turn (e.g. S1 CONFIRMED + S2 OPEN_ISSUE)
  for (const group of currentAudit.selected_groups) {
    const turnId = group.primary_turn_id;
    if (!turnId || !DECISION_RELEVANT_STATUSES.has(group.epistemic_status)) continue;
    const alreadyRepresented = chains.some((chain) =>
      chain.nodes.some(
        (node) =>
          node.turnId === turnId && node.epistemicStatus === group.epistemic_status,
      ),
    );
    if (alreadyRepresented) continue;
    const preview = resolveTurnPreview(
      turnId,
      byId,
      ordinalById,
      fallbackOrdinalByTurn.get(turnId),
    );
    chains.push({
      id: `${turnId}:${group.citation_tag}`,
      nodes: [
        {
          turnId,
          ordinal: preview.ordinal,
          role: preview.loaded ? preview.role : undefined,
          excerpt: preview.excerpt,
          createdAt: preview.createdAt,
          loaded: preview.loaded,
          epistemicStatus: group.epistemic_status,
          citationTag: group.citation_tag,
          selectedInCurrentTurn: true,
        },
      ],
      latestStatus: group.epistemic_status,
      hasSupersession: false,
      activeInCurrentTurn: true,
    });
  }

  chains.sort((left, right) => {
    if (left.activeInCurrentTurn !== right.activeInCurrentTurn) {
      return left.activeInCurrentTurn ? -1 : 1;
    }
    if (left.hasSupersession !== right.hasSupersession) {
      return left.hasSupersession ? -1 : 1;
    }
    const leftTailOrd = left.nodes[left.nodes.length - 1]?.ordinal ?? 0;
    const rightTailOrd = right.nodes[right.nodes.length - 1]?.ordinal ?? 0;
    return rightTailOrd - leftTailOrd;
  });

  let supersededCount = 0;
  for (const chain of chains) {
    for (const node of chain.nodes) {
      if (node.epistemicStatus === "SUPERSEDED") supersededCount += 1;
    }
  }

  return {
    chains,
    totalTrackedDecisions: Math.max(
      currentAudit.active_decisions_count,
      trackedIds.size - supersededCount,
    ),
    supersededCount,
  };
}

export function epistemicTone(status: EpistemicStatus): EpistemicTone {
  switch (status) {
    case "CONFIRMED":
      return "confirmed";
    case "PROPOSED":
    case "CONSIDERING":
      return "considering";
    case "SUPERSEDED":
    case "NEGATED":
      return "superseded";
    case "OPEN_ISSUE":
      return "issue";
    case "FACT":
    default:
      return "fact";
  }
}

export function epistemicBadgeClasses(status: EpistemicStatus): string {
  switch (epistemicTone(status)) {
    case "confirmed":
      return "border-emerald-600/35 bg-emerald-500/12 text-emerald-800 dark:border-emerald-400/35 dark:bg-emerald-400/15 dark:text-emerald-200";
    case "considering":
      return "border-amber-600/35 bg-amber-500/12 text-amber-800 dark:border-amber-400/35 dark:bg-amber-400/15 dark:text-amber-200";
    case "superseded":
      return "border-rose-600/35 bg-rose-500/12 text-rose-800 dark:border-rose-400/35 dark:bg-rose-400/15 dark:text-rose-200";
    case "issue":
      return "border-orange-600/35 bg-orange-500/12 text-orange-800 dark:border-orange-400/35 dark:bg-orange-400/15 dark:text-orange-200";
    case "fact":
    default:
      return "border-sky-600/35 bg-sky-500/12 text-sky-800 dark:border-sky-400/35 dark:bg-sky-400/15 dark:text-sky-200";
  }
}

export function sessionCitationPillClasses(
  status: EpistemicStatus,
  interactive = true,
): string {
  const base = epistemicBadgeClasses(status);
  if (!interactive) return `${base} cursor-default opacity-65`;
  switch (epistemicTone(status)) {
    case "confirmed":
      return `${base} cursor-pointer hover:bg-emerald-500/22 dark:hover:bg-emerald-400/25`;
    case "considering":
      return `${base} cursor-pointer hover:bg-amber-500/22 dark:hover:bg-amber-400/25`;
    case "superseded":
      return `${base} cursor-pointer line-through hover:bg-rose-500/22 dark:hover:bg-rose-400/25`;
    case "issue":
      return `${base} cursor-pointer hover:bg-orange-500/22 dark:hover:bg-orange-400/25`;
    case "fact":
    default:
      return `${base} cursor-pointer hover:bg-sky-500/22 dark:hover:bg-sky-400/25`;
  }
}

export function computeBudgetBreakdown(audit: SessionContextAudit): BudgetBreakdown {
  const budgetTokens = Math.max(0, audit.budget_tokens);
  const usedEvidenceTokens = Math.max(0, audit.used_evidence_tokens);
  const usedRecentTokens = Math.max(0, audit.used_recent_tokens);
  const totalUsedTokens = Math.max(
    usedEvidenceTokens + usedRecentTokens,
    audit.total_output_history_tokens,
  );
  const denom = budgetTokens > 0 ? budgetTokens : Math.max(1, totalUsedTokens);
  const evidencePercent = Math.min(100, (usedEvidenceTokens / denom) * 100);
  const recentPercent = Math.min(
    Math.max(0, 100 - evidencePercent),
    (usedRecentTokens / denom) * 100,
  );
  const totalPercent = Math.min(100, (totalUsedTokens / denom) * 100);

  return {
    budgetTokens,
    usedEvidenceTokens,
    usedRecentTokens,
    totalUsedTokens,
    remainingTokens: Math.max(0, budgetTokens - totalUsedTokens),
    evidencePercent,
    recentPercent,
    totalPercent,
    overBudget: budgetTokens > 0 && totalUsedTokens > budgetTokens,
  };
}

const CANONICAL_VIEW_ORDER = [
  "state",
  "graph",
  "hybrid",
  "hierarchy",
  "short_verbatim",
  "legacy",
] as const;

export function normalizeViewWeights(
  viewWeights: Record<string, number>,
): ViewWeightEntry[] {
  const entries = Object.entries(viewWeights).filter(
    ([, weight]) => Number.isFinite(weight) && weight > 0,
  );
  if (entries.length === 0) return [];
  const sum = entries.reduce((acc, [, weight]) => acc + weight, 0) || 1;
  return entries
    .map(([key, weight]) => ({
      key,
      weight,
      sharePercent: (weight / sum) * 100,
    }))
    .sort((left, right) => {
      if (Math.abs(right.weight - left.weight) > 1e-6) {
        return right.weight - left.weight;
      }
      const leftOrder = CANONICAL_VIEW_ORDER.indexOf(
        left.key as (typeof CANONICAL_VIEW_ORDER)[number],
      );
      const rightOrder = CANONICAL_VIEW_ORDER.indexOf(
        right.key as (typeof CANONICAL_VIEW_ORDER)[number],
      );
      return (leftOrder === -1 ? 99 : leftOrder) - (rightOrder === -1 ? 99 : rightOrder);
    });
}
