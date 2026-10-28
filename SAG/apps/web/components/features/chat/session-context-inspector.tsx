"use client";

import * as React from "react";
import Link from "next/link";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUpRight,
  CheckCircle2,
  CornerUpLeft,
  Database,
  FilterX,
  GitBranch,
  History,
  Layers,
  ShieldAlert,
  Sparkles,
} from "lucide-react";
import { useTranslations } from "next-intl";

import {
  computeBudgetBreakdown,
  epistemicBadgeClasses,
  extractSupersededByTurnId,
  normalizeViewWeights,
  type DecisionLineageModel,
} from "@/lib/session-context-presentation";
import type {
  EpistemicStatus,
  RejectedCandidateTrace,
  SessionCitation,
  SessionContextAudit,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

export interface SessionContextInspectorProps {
  audit: SessionContextAudit;
  sessionCitations: readonly SessionCitation[];
  decisionLineage: DecisionLineageModel;
  transitiveSources?: ReadonlyArray<{ id: string; name?: string }>;
  focusRef?: string | null;
  turnExcerptsById?: ReadonlyMap<string, { role: string; excerpt: string; ordinal?: number }>;
  onJumpToTurn?: (turnId: string) => void;
}

const KNOWN_CLOSURE_REASONS = [
  "direct_match",
  "decision_lineage_closure",
  "anaphoric_prev_neighbor_closure",
  "anaphoric_confirmation_closure",
  "qa_pair_neighbor_closure",
  "cross_view_bridge",
  "single_turn",
  "short_session_verbatim",
  "recent_continuity",
] as const;

const KNOWN_REJECTION_REASONS = [
  "SUPERSEDED_BY_NEWER_DECISION",
  "REDUNDANT_SEMANTIC_DUPLICATE",
  "TOKEN_BUDGET_CAP",
  "LOW_MARGINAL_UTILITY",
] as const;

const KNOWN_PERSISTENCE_STATUSES = [
  "hydrated_clean",
  "warm_clean",
  "delta_synced",
  "lazy_backfill",
  "drift_rebuilt",
  "qdrant_reconciled",
  "sync_failed",
  "qdrant_degraded",
  "memory_only",
  "legacy_bypass",
] as const;

const KNOWN_MEMORY_SCOPES = [
  "session_history",
  "knowledge_base",
  "hybrid_both",
  "full_session_audit",
] as const;

const KNOWN_TEMPORAL_MODES = [
  "atemporal",
  "current_effective",
  "past_point",
  "change_lineage",
] as const;

const KNOWN_VIEW_KEYS = [
  "state",
  "graph",
  "hybrid",
  "hierarchy",
  "short_verbatim",
  "legacy",
] as const;

function isOneOf<T extends string>(list: readonly T[], value: string): value is T {
  return (list as readonly string[]).includes(value);
}

function formatLatencyMs(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0ms";
  if (value >= 1000) return `${(value / 1000).toFixed(2)}s`;
  return value < 10 ? `${value.toFixed(1)}ms` : `${Math.round(value)}ms`;
}

function modeBadgeStyle(mode: string): string {
  switch (mode) {
    case "on":
      return "border-emerald-600/35 bg-emerald-500/12 text-emerald-800 dark:border-emerald-400/35 dark:bg-emerald-400/15 dark:text-emerald-200";
    case "shadow":
      return "border-amber-600/35 bg-amber-500/12 text-amber-800 dark:border-amber-400/35 dark:bg-amber-400/15 dark:text-amber-200";
    default:
      return "border-border bg-muted text-muted-foreground";
  }
}

function persistenceBadgeStyle(status: string): string {
  if (status.includes("failed") || status.includes("degraded")) {
    return "border-amber-600/35 bg-amber-500/12 text-amber-800 dark:border-amber-400/35 dark:bg-amber-400/15 dark:text-amber-200";
  }
  if (
    status === "hydrated_clean"
    || status === "warm_clean"
    || status === "delta_synced"
    || status === "lazy_backfill"
    || status === "drift_rebuilt"
    || status === "qdrant_reconciled"
  ) {
    return "border-emerald-600/35 bg-emerald-500/12 text-emerald-800 dark:border-emerald-400/35 dark:bg-emerald-400/15 dark:text-emerald-200";
  }
  return "border-border bg-muted text-muted-foreground";
}

function rejectionReasonStyle(code: string): string {
  switch (code) {
    case "SUPERSEDED_BY_NEWER_DECISION":
      return "border-rose-600/35 bg-rose-500/12 text-rose-800 dark:border-rose-400/35 dark:bg-rose-400/15 dark:text-rose-200";
    case "TOKEN_BUDGET_CAP":
      return "border-amber-600/35 bg-amber-500/12 text-amber-800 dark:border-amber-400/35 dark:bg-amber-400/15 dark:text-amber-200";
    case "REDUNDANT_SEMANTIC_DUPLICATE":
      return "border-sky-600/35 bg-sky-500/12 text-sky-800 dark:border-sky-400/35 dark:bg-sky-400/15 dark:text-sky-200";
    case "LOW_MARGINAL_UTILITY":
    default:
      return "border-border bg-muted text-muted-foreground";
  }
}

function viewBarColor(key: string): string {
  switch (key) {
    case "state":
      return "bg-emerald-500 dark:bg-emerald-400";
    case "graph":
      return "bg-sky-500 dark:bg-sky-400";
    case "hybrid":
      return "bg-indigo-500 dark:bg-indigo-400";
    case "hierarchy":
      return "bg-amber-500 dark:bg-amber-400";
    default:
      return "bg-primary/70";
  }
}

function EpistemicBadge({ status }: { status: EpistemicStatus }) {
  const t = useTranslations("SessionContextInspector");
  return (
    <span
      className={cn(
        "inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium leading-none",
        epistemicBadgeClasses(status),
      )}
    >
      {t(`epistemic.${status}`)}
    </span>
  );
}

export function SessionContextInspector({
  audit,
  sessionCitations,
  decisionLineage,
  transitiveSources,
  focusRef,
  turnExcerptsById,
  onJumpToTurn,
}: SessionContextInspectorProps) {
  const t = useTranslations("SessionContextInspector");
  const [activeTab, setActiveTab] = React.useState<"selected" | "rejected" | "lineage">("selected");
  const [reasonFilter, setReasonFilter] = React.useState<string>("ALL");
  const cardRefs = React.useRef<Map<string, HTMLElement>>(new Map());

  React.useEffect(() => {
    if (!focusRef) return;
    setActiveTab("selected");
    const timer = window.requestAnimationFrame(() => {
      const targetEl = cardRefs.current.get(focusRef);
      targetEl?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
    return () => window.cancelAnimationFrame(timer);
  }, [focusRef]);

  const budget = React.useMemo(() => computeBudgetBreakdown(audit), [audit]);
  const viewWeights = React.useMemo(
    () => normalizeViewWeights(audit.view_weights),
    [audit.view_weights],
  );

  const reasonCounts = React.useMemo(() => {
    const counts = new Map<string, number>();
    for (const candidate of audit.rejected_candidates) {
      counts.set(candidate.reason_code, (counts.get(candidate.reason_code) ?? 0) + 1);
    }
    return counts;
  }, [audit.rejected_candidates]);

  const filteredRejected = React.useMemo(() => {
    if (reasonFilter === "ALL") return audit.rejected_candidates;
    return audit.rejected_candidates.filter(
      (candidate) => candidate.reason_code === reasonFilter,
    );
  }, [audit.rejected_candidates, reasonFilter]);

  const renderClosureLabel = (reason: string) =>
    isOneOf(KNOWN_CLOSURE_REASONS, reason)
      ? t(`closureReasons.${reason}`)
      : reason;

  const renderRejectionLabel = (code: string) =>
    isOneOf(KNOWN_REJECTION_REASONS, code)
      ? t(`rejectionReasons.${code}`)
      : code;

  const renderPersistenceLabel = (status: string) =>
    isOneOf(KNOWN_PERSISTENCE_STATUSES, status)
      ? t(`persistence.${status}`)
      : status;

  const renderScopeLabel = (scope: string) =>
    isOneOf(KNOWN_MEMORY_SCOPES, scope)
      ? t(`memoryScopes.${scope}`)
      : scope;

  const renderTemporalLabel = (mode: string) =>
    isOneOf(KNOWN_TEMPORAL_MODES, mode)
      ? t(`temporalModes.${mode}`)
      : mode;

  const renderViewLabel = (viewKey: string) =>
    isOneOf(KNOWN_VIEW_KEYS, viewKey)
      ? t(`views.${viewKey}`)
      : viewKey;

  return (
    <div className="flex min-w-0 flex-col gap-3.5 text-xs">
      {/* 1. Run Telemetry & Persistence Header */}
      <section className="rounded-xl border bg-card p-3 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <span
              className={cn(
                "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 font-mono text-[10px] font-semibold uppercase",
                modeBadgeStyle(audit.mode),
              )}
            >
              <Sparkles className="size-3" aria-hidden="true" />
              {t(`modes.${audit.mode}`)}
            </span>
            <span className="inline-flex items-center rounded-md border bg-muted/60 px-2 py-0.5 font-mono text-[10px] font-medium text-foreground/85">
              {audit.ablation}
            </span>
          </div>

          <span
            className={cn(
              "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[10px] font-medium",
              persistenceBadgeStyle(audit.persistence_status),
            )}
            title={audit.persistence_status}
          >
            <Database className="size-3" aria-hidden="true" />
            {renderPersistenceLabel(audit.persistence_status)}
          </span>
        </div>

        {audit.mode === "shadow" && (
          <p className="mt-2 rounded-md border border-amber-500/25 bg-amber-500/10 px-2.5 py-1.5 text-[11px] leading-relaxed text-amber-900 dark:border-amber-400/25 dark:bg-amber-400/10 dark:text-amber-100">
            {t("shadowModeNotice")}
          </p>
        )}

        {/* 4 KPI Strip */}
        <div className="mt-3 grid grid-cols-2 gap-2">
          <div className="rounded-lg border bg-muted/25 px-2.5 py-2">
            <span className="block truncate text-[10px] text-muted-foreground">
              {t("kpi.turnsAndEpisodes")}
            </span>
            <span className="mt-0.5 block font-mono text-sm font-semibold tabular-nums text-foreground">
              {audit.total_indexed_turns}
              <span className="text-xs font-normal text-muted-foreground">
                {" / "}
                {audit.episode_count}
              </span>
            </span>
          </div>

          <div className="rounded-lg border bg-muted/25 px-2.5 py-2">
            <span className="block truncate text-[10px] text-muted-foreground">
              {t("kpi.activeDecisions")}
            </span>
            <span className="mt-0.5 block font-mono text-sm font-semibold tabular-nums text-foreground">
              {audit.active_decisions_count}
            </span>
          </div>

          <div className="rounded-lg border bg-muted/25 px-2.5 py-2">
            <span className="block truncate text-[10px] text-muted-foreground">
              {t("kpi.llmMemoryCalls")}
            </span>
            <span
              className={cn(
                "mt-0.5 block font-mono text-sm font-semibold tabular-nums",
                audit.llm_memory_calls === 0
                  ? "text-emerald-700 dark:text-emerald-300"
                  : "text-foreground",
              )}
            >
              {audit.llm_memory_calls}
            </span>
          </div>

          <div className="rounded-lg border bg-muted/25 px-2.5 py-2">
            <span className="block truncate text-[10px] text-muted-foreground">
              {t("kpi.latency")}
            </span>
            <span className="mt-0.5 block font-mono text-sm font-semibold tabular-nums text-foreground">
              {formatLatencyMs(audit.latency_ms)}
            </span>
          </div>
        </div>

        {/* Routing & Scope Metadata */}
        <dl className="mt-2.5 flex flex-wrap gap-x-3 gap-y-1 border-t pt-2.5 text-[11px] text-muted-foreground">
          <div className="inline-flex items-center gap-1">
            <dt className="text-muted-foreground/75">{t("meta.scope")}:</dt>
            <dd className="font-medium text-foreground/85">
              {renderScopeLabel(audit.memory_scope)}
            </dd>
          </div>
          <div className="inline-flex items-center gap-1">
            <dt className="text-muted-foreground/75">{t("meta.temporal")}:</dt>
            <dd className="font-medium text-foreground/85">
              {renderTemporalLabel(audit.temporal_mode)}
            </dd>
          </div>
          <div className="inline-flex items-center gap-1">
            <dt className="text-muted-foreground/75">{t("meta.router")}:</dt>
            <dd className="font-mono text-foreground/85">
              {audit.router_model}
              {audit.router_fallback_used ? ` (${t("meta.routerFallback")})` : ""}
            </dd>
          </div>
          {(audit.persisted_units_written > 0 || audit.qdrant_points_upserted > 0) && (
            <div className="inline-flex items-center gap-1 tabular-nums">
              <dt className="text-muted-foreground/75">{t("meta.syncedUnits")}:</dt>
              <dd className="font-mono text-foreground/85">
                PG +{audit.persisted_units_written} · Qdrant +{audit.qdrant_points_upserted}
              </dd>
            </div>
          )}
        </dl>

        {transitiveSources && transitiveSources.length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5 border-t pt-2">
            <span className="text-[10px] font-medium text-muted-foreground">
              {t("meta.transitiveSources")}:
            </span>
            {transitiveSources.map((src) => (
              <Link
                key={src.id}
                href={`/knowledge/${src.id}`}
                className="inline-flex max-w-full items-center gap-1 truncate rounded-md border bg-muted/40 px-2 py-0.5 text-[10px] font-medium text-foreground/85 transition-colors hover:bg-muted"
              >
                <span className="truncate">{src.name || src.id.slice(0, 8)}</span>
                <ArrowUpRight className="size-2.5 shrink-0" aria-hidden="true" />
              </Link>
            ))}
          </div>
        )}
      </section>

      {/* 2. Token Budget Bar & Partial Coverage Warning */}
      <section className="rounded-xl border bg-card p-3 shadow-sm">
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs font-semibold text-foreground">
            {t("budget.title")}
          </span>
          <span className="font-mono text-[11px] font-medium tabular-nums text-foreground/85">
            {budget.totalUsedTokens} / {budget.budgetTokens} {t("budget.tokensUnit")}{" "}
            <span className="text-muted-foreground">
              ({Math.round(budget.totalPercent)}%)
            </span>
          </span>
        </div>

        <div
          role="meter"
          aria-label={t("budget.title")}
          aria-valuemin={0}
          aria-valuemax={Math.max(1, budget.budgetTokens)}
          aria-valuenow={budget.totalUsedTokens}
          className="mt-2 flex h-2.5 w-full overflow-hidden rounded-full bg-muted"
        >
          {budget.evidencePercent > 0 && (
            <div
              style={{ width: `${budget.evidencePercent}%` }}
              className="h-full bg-emerald-500 transition-all dark:bg-emerald-400"
              title={`${t("budget.evidence")}: ${budget.usedEvidenceTokens}`}
            />
          )}
          {budget.recentPercent > 0 && (
            <div
              style={{ width: `${budget.recentPercent}%` }}
              className="h-full bg-sky-500 transition-all dark:bg-sky-400"
              title={`${t("budget.recent")}: ${budget.usedRecentTokens}`}
            />
          )}
        </div>

        <div className="mt-2 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          <div className="flex flex-wrap items-center gap-3">
            <span className="inline-flex items-center gap-1.5 tabular-nums">
              <span className="size-2 rounded-full bg-emerald-500 dark:bg-emerald-400" aria-hidden="true" />
              {t("budget.evidence")}:{" "}
              <strong className="font-mono font-medium text-foreground/85">
                {budget.usedEvidenceTokens}
              </strong>
            </span>
            <span className="inline-flex items-center gap-1.5 tabular-nums">
              <span className="size-2 rounded-full bg-sky-500 dark:bg-sky-400" aria-hidden="true" />
              {t("budget.recent")}:{" "}
              <strong className="font-mono font-medium text-foreground/85">
                {budget.usedRecentTokens}
              </strong>
            </span>
          </div>
          <span className="font-mono text-[10px] tabular-nums text-muted-foreground">
            {t("budget.remaining", { count: budget.remainingTokens })}
          </span>
        </div>

        {audit.partial_coverage && (
          <div
            role="status"
            className="mt-2.5 flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-2.5 py-2 text-[11px] text-amber-900 dark:border-amber-400/30 dark:bg-amber-400/10 dark:text-amber-100"
          >
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
            <span>{t("budget.partialCoverageWarning")}</span>
          </div>
        )}
      </section>

      {/* 3. Three Audit Tabs */}
      <Tabs
        value={activeTab}
        onValueChange={(value) => setActiveTab(value as "selected" | "rejected" | "lineage")}
        className="flex min-w-0 flex-col"
      >
        <TabsList className="grid w-full shrink-0 grid-cols-3">
          <TabsTrigger value="selected" className="min-w-0 gap-1 px-1.5 text-xs">
            <span className="truncate">{t("tabs.selected")}</span>
            <span className="shrink-0 rounded bg-muted px-1 py-px font-mono text-[10px] tabular-nums text-muted-foreground">
              {sessionCitations.length}
            </span>
          </TabsTrigger>
          <TabsTrigger value="rejected" className="min-w-0 gap-1 px-1.5 text-xs">
            <span className="truncate">{t("tabs.rejected")}</span>
            <span className="shrink-0 rounded bg-muted px-1 py-px font-mono text-[10px] tabular-nums text-muted-foreground">
              {audit.rejected_candidates.length}
            </span>
          </TabsTrigger>
          <TabsTrigger value="lineage" className="min-w-0 gap-1 px-1.5 text-xs">
            <span className="truncate">{t("tabs.lineage")}</span>
            <span className="shrink-0 rounded bg-muted px-1 py-px font-mono text-[10px] tabular-nums text-muted-foreground">
              {decisionLineage.chains.length}
            </span>
          </TabsTrigger>
        </TabsList>

        {/* TAB 1: Selected Evidence & Multi-View Weights */}
        <TabsContent value="selected" className="mt-3 flex min-w-0 flex-col gap-3">
          {viewWeights.length > 0 && (
            <section className="rounded-xl border bg-card p-3 shadow-sm">
              <div className="flex flex-col gap-0.5">
                <span className="text-xs font-semibold text-foreground">
                  {t("views.title")}
                </span>
                <span className="text-[10px] text-muted-foreground">
                  {t("views.subtitle")}
                </span>
              </div>
              <div className="mt-2 flex h-2 w-full overflow-hidden rounded-full bg-muted">
                {viewWeights.map((entry) => (
                  <div
                    key={entry.key}
                    style={{ width: `${entry.sharePercent}%` }}
                    className={cn("h-full transition-all", viewBarColor(entry.key))}
                    title={`${renderViewLabel(entry.key)}: ${entry.weight.toFixed(2)}`}
                  />
                ))}
              </div>
              <div className="mt-2.5 grid grid-cols-1 gap-1.5">
                {viewWeights.map((entry) => (
                  <div
                    key={entry.key}
                    className="flex items-center justify-between gap-1.5 rounded-md border bg-muted/25 px-2 py-1"
                  >
                    <span className="inline-flex min-w-0 items-center gap-1.5 truncate text-[11px] text-foreground/85">
                      <span
                        className={cn("size-2 shrink-0 rounded-full", viewBarColor(entry.key))}
                        aria-hidden="true"
                      />
                      <span className="truncate">{renderViewLabel(entry.key)}</span>
                    </span>
                    <span className="shrink-0 font-mono text-[10px] tabular-nums text-muted-foreground">
                      {entry.weight.toFixed(2)} ({Math.round(entry.sharePercent)}%)
                    </span>
                  </div>
                ))}
              </div>
            </section>
          )}

          {sessionCitations.length === 0 ? (
            <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed bg-muted/15 px-4 py-8 text-center">
              <Layers className="size-6 text-muted-foreground/70" aria-hidden="true" />
              <p className="text-xs font-medium text-foreground">{t("selected.emptyTitle")}</p>
              <p className="max-w-xs text-[11px] leading-relaxed text-muted-foreground">
                {t("selected.emptyDescription")}
              </p>
            </div>
          ) : (
            <div className="flex flex-col gap-2.5">
              {sessionCitations.map((item) => {
                const isFocused = focusRef === item.ref;
                return (
                  <article
                    key={item.ref}
                    ref={(node) => {
                      if (node) cardRefs.current.set(item.ref, node);
                      else cardRefs.current.delete(item.ref);
                    }}
                    className={cn(
                      "rounded-xl border bg-card p-3 shadow-sm transition-colors",
                      isFocused && "ring-2 ring-primary ring-offset-1 ring-offset-background",
                    )}
                  >
                    <div className="flex flex-wrap items-center justify-between gap-1.5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span
                          className={cn(
                            "inline-flex h-5 min-w-[26px] items-center justify-center rounded-md border px-1.5 font-mono text-[10px] font-semibold tabular-nums",
                            epistemicBadgeClasses(item.epistemicStatus),
                          )}
                        >
                          {item.ref}
                        </span>
                        <EpistemicBadge status={item.epistemicStatus} />
                        <span className="inline-flex items-center rounded border bg-muted/50 px-1.5 py-0.5 text-[10px] text-muted-foreground">
                          {renderClosureLabel(item.closureReason)}
                        </span>
                        {item.mappedInAnswer && (
                          <span className="inline-flex items-center gap-0.5 rounded border border-emerald-600/30 bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-medium text-emerald-800 dark:border-emerald-400/30 dark:bg-emerald-400/15 dark:text-emerald-200">
                            <CheckCircle2 className="size-2.5" aria-hidden="true" />
                            {t("selected.citedInAnswer")}
                          </span>
                        )}
                      </div>
                      {typeof item.tokenCost === "number" && (
                        <span className="font-mono text-[10px] tabular-nums text-muted-foreground">
                          {item.tokenCost} {t("budget.tokensUnit")}
                        </span>
                      )}
                    </div>

                    {item.kind === "evidence_group" && (
                      <div className="mt-2 grid grid-cols-2 gap-1.5 rounded-lg border bg-muted/20 p-2">
                        <div>
                          <span className="block text-[10px] text-muted-foreground">
                            {t("selected.utility")}
                          </span>
                          <span className="font-mono text-[11px] font-semibold tabular-nums text-foreground">
                            {(item.utilityScore ?? 0).toFixed(3)}
                          </span>
                        </div>
                        <div>
                          <span className="block text-[10px] text-muted-foreground">
                            {t("selected.coverageGain")}
                          </span>
                          <span className="font-mono text-[11px] font-semibold tabular-nums text-emerald-700 dark:text-emerald-300">
                            +{(item.coverageGain ?? 0).toFixed(2)}
                          </span>
                        </div>
                        <div>
                          <span className="block text-[10px] text-muted-foreground">
                            {t("selected.redundancy")}
                          </span>
                          <span className="font-mono text-[11px] font-semibold tabular-nums text-muted-foreground">
                            -{(item.redundancyPenalty ?? 0).toFixed(2)}
                          </span>
                        </div>
                        <div>
                          <span className="block text-[10px] text-muted-foreground">
                            {t("selected.gainPerToken")}
                          </span>
                          <span className="font-mono text-[11px] font-semibold tabular-nums text-foreground">
                            {(item.marginalGainPerToken ?? 0).toFixed(4)}
                          </span>
                        </div>
                      </div>
                    )}

                    <div className="mt-2.5 divide-y rounded-lg border bg-background/60">
                      {item.turns.map((turn) => (
                        <div key={turn.id} className="p-2">
                          <div className="flex items-center justify-between gap-2">
                            <span className="font-mono text-[10px] font-medium tabular-nums text-muted-foreground">
                              {turn.ordinal
                                ? t("turnOrdinalRole", {
                                    ordinal: turn.ordinal,
                                    role:
                                      turn.role === "assistant"
                                        ? t("roles.assistant")
                                        : t("roles.user"),
                                  })
                                : `#${turn.id.slice(0, 8)}`}
                            </span>
                            {onJumpToTurn && (
                              <button
                                type="button"
                                onClick={() => onJumpToTurn(turn.id)}
                                className="inline-flex h-5 cursor-pointer items-center gap-1 rounded px-1.5 text-[10px] font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                              >
                                <CornerUpLeft className="size-2.5" aria-hidden="true" />
                                {t("jumpToTurn")}
                              </button>
                            )}
                          </div>
                          <p className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-foreground/85">
                            {turn.excerpt || t("turnOlderUnloaded")}
                          </p>
                        </div>
                      ))}
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </TabsContent>

        {/* TAB 2: Rejected Candidates Audit */}
        <TabsContent value="rejected" className="mt-3 flex min-w-0 flex-col gap-3">
          {audit.rejected_candidates.length === 0 ? (
            <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed bg-muted/15 px-4 py-8 text-center">
              <ShieldAlert className="size-6 text-muted-foreground/70" aria-hidden="true" />
              <p className="text-xs font-medium text-foreground">{t("rejected.emptyTitle")}</p>
              <p className="max-w-xs text-[11px] leading-relaxed text-muted-foreground">
                {t("rejected.emptyDescription")}
              </p>
            </div>
          ) : (
            <>
              <div
                role="group"
                aria-label={t("rejected.filterAria")}
                className="flex flex-wrap gap-1.5"
              >
                <button
                  type="button"
                  onClick={() => setReasonFilter("ALL")}
                  className={cn(
                    "inline-flex h-6 cursor-pointer items-center gap-1 rounded-full border px-2.5 text-[10px] font-medium tabular-nums transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    reasonFilter === "ALL"
                      ? "border-foreground bg-foreground text-background"
                      : "bg-card text-muted-foreground hover:bg-muted hover:text-foreground",
                  )}
                >
                  {t("rejected.filterAll")} ({audit.rejected_candidates.length})
                </button>
                {KNOWN_REJECTION_REASONS.map((code) => {
                  const count = reasonCounts.get(code) ?? 0;
                  if (count === 0) return null;
                  const selected = reasonFilter === code;
                  return (
                    <button
                      key={code}
                      type="button"
                      onClick={() => setReasonFilter(code)}
                      className={cn(
                        "inline-flex h-6 cursor-pointer items-center gap-1 rounded-full border px-2.5 text-[10px] font-medium tabular-nums transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                        selected
                          ? "border-foreground bg-foreground text-background"
                          : "bg-card text-muted-foreground hover:bg-muted hover:text-foreground",
                      )}
                    >
                      {renderRejectionLabel(code)} ({count})
                    </button>
                  );
                })}
              </div>

              {filteredRejected.length === 0 ? (
                <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed bg-muted/15 px-4 py-6 text-center">
                  <FilterX className="size-5 text-muted-foreground" aria-hidden="true" />
                  <p className="text-xs text-muted-foreground">{t("rejected.noFilterMatches")}</p>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-7 text-xs"
                    onClick={() => setReasonFilter("ALL")}
                  >
                    {t("rejected.resetFilter")}
                  </Button>
                </div>
              ) : (
                <div className="flex flex-col gap-2">
                  {filteredRejected.map((candidate: RejectedCandidateTrace, idx) => {
                    const supersededByTurnId = extractSupersededByTurnId(candidate.detail);
                    const loadedTurn = turnExcerptsById?.get(candidate.turn_id);
                    const displayOrdinal = loadedTurn?.ordinal ?? candidate.turn_ordinal + 1;
                    return (
                      <article
                        key={`${candidate.turn_id}-${candidate.reason_code}-${idx}`}
                        className="rounded-xl border bg-card p-3 shadow-sm"
                      >
                        <div className="flex flex-wrap items-center justify-between gap-1.5">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="inline-flex items-center rounded border bg-muted/60 px-1.5 py-0.5 font-mono text-[10px] font-semibold tabular-nums text-foreground/85">
                              {t("turnNumber", { ordinal: displayOrdinal })}
                            </span>
                            <EpistemicBadge status={candidate.epistemic_status} />
                            <span
                              className={cn(
                                "inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium",
                                rejectionReasonStyle(candidate.reason_code),
                              )}
                            >
                              {renderRejectionLabel(candidate.reason_code)}
                            </span>
                          </div>
                          <span className="font-mono text-[10px] tabular-nums text-muted-foreground">
                            {t("rejected.rawScore")}: {candidate.raw_score.toFixed(3)}
                          </span>
                        </div>

                        {loadedTurn?.excerpt && (
                          <p className="mt-2 line-clamp-2 rounded-md border bg-muted/20 px-2 py-1.5 text-[11px] leading-relaxed text-foreground/80">
                            {loadedTurn.excerpt}
                          </p>
                        )}

                        {candidate.detail && (
                          <p className="mt-1.5 font-mono text-[10px] text-muted-foreground">
                            {candidate.detail}
                          </p>
                        )}

                        {onJumpToTurn && (
                          <div className="mt-2 flex flex-wrap items-center gap-2">
                            <button
                              type="button"
                              onClick={() => onJumpToTurn(candidate.turn_id)}
                              className="inline-flex h-6 cursor-pointer items-center gap-1 rounded-md border bg-muted/40 px-2 text-[10px] font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            >
                              <History className="size-3" aria-hidden="true" />
                              {t("jumpToTurn")}
                            </button>
                            {supersededByTurnId && (
                              <button
                                type="button"
                                onClick={() => onJumpToTurn(supersededByTurnId)}
                                className="inline-flex h-6 cursor-pointer items-center gap-1 rounded-md border border-emerald-600/35 bg-emerald-500/10 px-2 text-[10px] font-medium text-emerald-800 transition-colors hover:bg-emerald-500/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring dark:border-emerald-400/35 dark:bg-emerald-400/15 dark:text-emerald-200"
                              >
                                <CornerUpLeft className="size-3" aria-hidden="true" />
                                {t("rejected.jumpToSuperseding")}
                              </button>
                            )}
                          </div>
                        )}
                      </article>
                    );
                  })}
                </div>
              )}
            </>
          )}
        </TabsContent>

        {/* TAB 3: Decision Superseding Lineage */}
        <TabsContent value="lineage" className="mt-3 flex min-w-0 flex-col gap-3">
          <div className="grid grid-cols-2 gap-2">
            <div className="rounded-xl border bg-card px-3 py-2 shadow-sm">
              <span className="block text-[10px] text-muted-foreground">
                {t("lineage.activeCount")}
              </span>
              <span className="font-mono text-sm font-semibold tabular-nums text-emerald-700 dark:text-emerald-300">
                {decisionLineage.totalTrackedDecisions}
              </span>
            </div>
            <div className="rounded-xl border bg-card px-3 py-2 shadow-sm">
              <span className="block text-[10px] text-muted-foreground">
                {t("lineage.supersededCount")}
              </span>
              <span className="font-mono text-sm font-semibold tabular-nums text-rose-700 dark:text-rose-300">
                {decisionLineage.supersededCount}
              </span>
            </div>
          </div>

          {decisionLineage.chains.length === 0 ? (
            <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed bg-muted/15 px-4 py-8 text-center">
              <GitBranch className="size-6 text-muted-foreground/70" aria-hidden="true" />
              <p className="text-xs font-medium text-foreground">{t("lineage.emptyTitle")}</p>
              <p className="max-w-xs text-[11px] leading-relaxed text-muted-foreground">
                {t("lineage.emptyDescription")}
              </p>
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              {decisionLineage.chains.map((chain, chainIdx) => (
                <section
                  key={chain.id}
                  className="rounded-xl border bg-card p-3 shadow-sm"
                >
                  <div className="flex items-center justify-between gap-2 border-b pb-2">
                    <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-foreground">
                      <GitBranch className="size-3.5 text-primary" aria-hidden="true" />
                      {chain.hasSupersession
                        ? t("lineage.supersessionChainTitle", { index: chainIdx + 1 })
                        : t("lineage.decisionStateTitle", { index: chainIdx + 1 })}
                    </span>
                    <EpistemicBadge status={chain.latestStatus} />
                  </div>

                  <ol className="mt-2.5 flex flex-col gap-1.5">
                    {chain.nodes.map((node, nodeIdx) => {
                      const isSuperseded =
                        node.epistemicStatus === "SUPERSEDED"
                        || node.epistemicStatus === "NEGATED";
                      return (
                        <React.Fragment key={node.turnId}>
                          <li
                            className={cn(
                              "rounded-lg border p-2.5 transition-colors",
                              isSuperseded
                                ? "border-rose-500/25 bg-rose-500/[0.04] dark:border-rose-400/25 dark:bg-rose-400/[0.05]"
                                : node.epistemicStatus === "CONFIRMED"
                                  ? "border-emerald-500/30 bg-emerald-500/[0.06] dark:border-emerald-400/30 dark:bg-emerald-400/[0.08]"
                                  : "bg-background/65",
                            )}
                          >
                            <div className="flex flex-wrap items-center justify-between gap-1.5">
                              <div className="flex flex-wrap items-center gap-1.5">
                                <span
                                  className={cn(
                                    "font-mono text-[10px] font-semibold tabular-nums",
                                    isSuperseded
                                      ? "line-through text-muted-foreground"
                                      : "text-foreground",
                                  )}
                                >
                                  {node.ordinal
                                    ? t("turnNumber", { ordinal: node.ordinal })
                                    : `#${node.turnId.slice(0, 8)}`}
                                </span>
                                <EpistemicBadge status={node.epistemicStatus} />
                                {node.citationTag && (
                                  <span className="inline-flex items-center rounded border bg-background px-1.5 py-0.5 font-mono text-[10px] font-semibold tabular-nums text-foreground">
                                    [{node.citationTag}]
                                  </span>
                                )}
                              </div>
                              {onJumpToTurn && (
                                <button
                                  type="button"
                                  onClick={() => onJumpToTurn(node.turnId)}
                                  className="inline-flex h-5 cursor-pointer items-center gap-1 rounded px-1.5 text-[10px] font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                                >
                                  <CornerUpLeft className="size-2.5" aria-hidden="true" />
                                  {t("jumpToTurn")}
                                </button>
                              )}
                            </div>
                            <p
                              className={cn(
                                "mt-1.5 whitespace-pre-wrap break-words text-[11px] leading-relaxed",
                                isSuperseded
                                  ? "text-muted-foreground"
                                  : "text-foreground/85",
                              )}
                            >
                              {node.excerpt || t("turnOlderUnloaded")}
                            </p>
                          </li>

                          {nodeIdx < chain.nodes.length - 1 && (
                            <div
                              aria-hidden="true"
                              className="flex items-center gap-1.5 pl-3 text-[10px] text-muted-foreground"
                            >
                              <ArrowDown className="size-3 text-primary" />
                              <span>{t("lineage.supersededTransition")}</span>
                            </div>
                          )}
                        </React.Fragment>
                      );
                    })}
                  </ol>
                </section>
              ))}
            </div>
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}
