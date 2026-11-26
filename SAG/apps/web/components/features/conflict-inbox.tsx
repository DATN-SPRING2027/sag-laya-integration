"use client";

import * as React from "react";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import {
  Archive,
  ArrowLeft,
  Calendar,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  FileText,
  History,
  RotateCcw,
  RotateCw,
  Scale,
  Search,
  Shield,
  Split,
  TriangleAlert,
  X,
} from "lucide-react";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import {
  CONFLICT_SUBTYPES,
  authorityTierMeta,
  canReopenConflict,
  canResolveConflict,
  compareAuthorityPrecedence,
  conflictStatusTone,
  conflictSubtypeTone,
  formatConfidencePct,
  formatConflictDate,
  formatPageRange,
  isArchivedConflict,
  tabToStatuses,
  type ConflictFilterTab,
} from "@/lib/conflict-presentation";
import { DEFAULT_TIME_ZONE, formatDateTime, relativeTime } from "@/lib/format";
import type {
  ConflictItem,
  ConflictResolutionStatus,
  ConflictResolveAction,
  ConflictSide,
  ConflictSubtype,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { EmptyState } from "@/components/features/empty-state";
import { PageHeader } from "@/components/features/page-header";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

const PAGE_LIMIT = 20;
const MAX_REASON_LENGTH = 2000;

type PendingMutation = ConflictResolveAction | "REOPEN";

const KNOWN_STATUSES: ReadonlySet<string> = new Set<ConflictResolutionStatus>([
  "PENDING_REVIEW",
  "NEEDS_REVALIDATION",
  "ACTIVE",
  "DISMISSED_SCOPED",
  "REVOKED_WINNER",
  "SUPERSEDED_BY_UPDATE",
]);

function isKnownStatus(value: string): value is ConflictResolutionStatus {
  return KNOWN_STATUSES.has(value);
}

interface ConflictSidePanelProps {
  side: ConflictSide;
  role: "a" | "b";
  status: ConflictResolutionStatus;
  locale: string;
}

function ConflictSidePanel({
  side,
  role,
  status,
  locale,
}: ConflictSidePanelProps) {
  const t = useTranslations("ConflictInbox");
  const tierMeta = authorityTierMeta(side.authority_tier);
  const effectiveDate = formatConflictDate(side.effective_from, locale);
  const pageRange = formatPageRange(side.page_from, side.page_to);

  const roleConfig = React.useMemo(() => {
    if (status === "ACTIVE") {
      return role === "b"
        ? {
            label: t("sides.activeWinner"),
            containerClass:
              "border-emerald-500/40 bg-emerald-500/[0.03] dark:bg-emerald-500/[0.05]",
            badgeClass:
              "border-emerald-500/35 bg-emerald-500/12 text-emerald-800 dark:text-emerald-300",
          }
        : {
            label: t("sides.superseded"),
            containerClass: "border-border bg-muted/25",
            badgeClass: "border-border bg-muted text-muted-foreground",
          };
    }
    if (status === "DISMISSED_SCOPED") {
      return {
        label: role === "a" ? t("sides.scopedA") : t("sides.scopedB"),
        containerClass: "border-sky-500/30 bg-sky-500/[0.02]",
        badgeClass:
          "border-sky-500/30 bg-sky-500/10 text-sky-800 dark:text-sky-300",
      };
    }
    if (isArchivedConflict(status)) {
      return {
        label: role === "a" ? t("sides.sideA") : t("sides.sideB"),
        containerClass: "border-border bg-muted/20",
        badgeClass: "border-border bg-muted text-muted-foreground",
      };
    }
    return role === "b"
      ? {
          label: t("sides.proposedB"),
          containerClass: "border-foreground/20 bg-card",
          badgeClass: "border-foreground/20 bg-foreground/8 text-foreground",
        }
      : {
          label: t("sides.baselineA"),
          containerClass: "border-border bg-card",
          badgeClass: "border-border bg-muted text-muted-foreground",
        };
  }, [role, status, t]);

  return (
    <section
      data-side={role}
      className={cn(
        "flex min-w-0 flex-col rounded-md border p-3.5 transition-colors",
        roleConfig.containerClass,
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span
          className={cn(
            "inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium",
            roleConfig.badgeClass,
          )}
        >
          {roleConfig.label}
        </span>
        {tierMeta && (
          <span
            className={cn(
              "inline-flex items-center rounded-full border px-2 py-0.5 font-mono text-[11px] font-medium",
              tierMeta.toneClass,
            )}
          >
            {t("tiers.badge", {
              code: tierMeta.code,
              label: t(`tiers.${tierMeta.i18nKey}`),
            })}
          </span>
        )}
      </div>

      <div className="mt-2.5 flex flex-wrap items-baseline gap-2">
        <h3 className="min-w-0 break-words text-sm font-medium leading-snug text-foreground">
          {side.document_name}
        </h3>
        <span className="shrink-0 rounded border border-border bg-background px-1.5 py-0.5 font-mono text-[11px] tabular-nums text-muted-foreground">
          {t("sides.version", { version: side.version_no })}
        </span>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
        {effectiveDate && (
          <span className="inline-flex items-center gap-1 tabular-nums">
            <Calendar className="size-3 shrink-0" />
            {t("sides.effectiveFrom", { date: effectiveDate })}
          </span>
        )}
        {pageRange && (
          <span className="inline-flex items-center gap-1 font-mono tabular-nums">
            <FileText className="size-3 shrink-0" />
            {pageRange}
          </span>
        )}
        {side.security_partition_id && (
          <span className="inline-flex items-center gap-1 font-mono">
            <Shield className="size-3 shrink-0" />
            {t("sides.partition", { id: side.security_partition_id })}
          </span>
        )}
      </div>

      {side.section_path && (
        <p
          className="mt-1.5 truncate font-mono text-[11px] text-muted-foreground"
          title={side.section_path}
        >
          {side.section_path}
        </p>
      )}

      <div className="mt-2.5 max-h-56 overflow-y-auto whitespace-pre-wrap break-words rounded-md border bg-background/85 p-3 text-sm leading-relaxed text-foreground">
        {side.text_snapshot || t("sides.noExcerpt")}
      </div>
    </section>
  );
}

interface ConflictCardProps {
  item: ConflictItem;
  locale: string;
  timezone: string;
  reasonDraft: string;
  pendingAction: PendingMutation | undefined;
  onReasonChange: (id: string, value: string) => void;
  onResolve: (id: string, action: ConflictResolveAction) => void;
  onReopen: (id: string) => void;
}

function ConflictCard({
  item,
  locale,
  timezone,
  reasonDraft,
  pendingAction,
  onReasonChange,
  onResolve,
  onReopen,
}: ConflictCardProps) {
  const t = useTranslations("ConflictInbox");
  const [auditOpen, setAuditOpen] = React.useState(false);

  const isMutating = pendingAction !== undefined;
  const resolvable = canResolveConflict(item.status);
  const reopenable = canReopenConflict(item.status);
  const archived = isArchivedConflict(item.status);

  const tierA = authorityTierMeta(item.side_a.authority_tier);
  const tierB = authorityTierMeta(item.side_b.authority_tier);
  const precedence = compareAuthorityPrecedence(item.side_a, item.side_b);

  const precedenceLabel = React.useMemo(() => {
    if (!tierA || !tierB || precedence === "incomplete") return null;
    if (precedence === "b_higher") {
      return t("precedence.bHigher", {
        codeA: tierA.code,
        codeB: tierB.code,
      });
    }
    if (precedence === "a_higher") {
      return t("precedence.aHigher", {
        codeA: tierA.code,
        codeB: tierB.code,
      });
    }
    return t("precedence.equal", { codeA: tierA.code, codeB: tierB.code });
  }, [precedence, t, tierA, tierB]);

  const effectiveValidToFormatted = formatConflictDate(
    item.effective_valid_to,
    locale,
  );
  const resolvedAtFormatted = item.resolved_at
    ? formatDateTime(item.resolved_at, timezone, locale)
    : null;
  const updatedRelative = relativeTime(item.updated_at, timezone, locale);
  const updatedExact = formatDateTime(item.updated_at, timezone, locale);

  const formatAuditStatus = (value: string) =>
    isKnownStatus(value) ? t(`statuses.${value}`) : value;

  return (
    <article
      data-conflict-id={item.id}
      className="overflow-hidden rounded-lg border bg-card text-card-foreground shadow-soft"
    >
      {/* Header strip */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b bg-muted/30 px-4 py-3">
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge className={conflictStatusTone(item.status)}>
            {t(`statuses.${item.status}`)}
          </Badge>
          <Badge className={conflictSubtypeTone(item.conflict_subtype)}>
            {t(`subtypes.${item.conflict_subtype}`)}
          </Badge>
          <span className="inline-flex items-center rounded border border-border bg-background px-2 py-0.5 font-mono text-[11px] text-muted-foreground">
            {t(`modes.${item.resolution_mode}`)}
          </span>
          {precedenceLabel && (
            <span className="inline-flex items-center gap-1 rounded border border-border/80 bg-background px-2 py-0.5 font-mono text-[11px] text-foreground/85">
              <Scale className="size-3 text-muted-foreground" />
              {precedenceLabel}
            </span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span className="rounded border border-border/70 bg-background px-1.5 py-0.5 font-mono text-[11px] tabular-nums">
            {t("metrics.confidence", {
              value: formatConfidencePct(item.confidence),
            })}
          </span>
          <span className="rounded border border-border/70 bg-background px-1.5 py-0.5 font-mono text-[11px] tabular-nums">
            {t("metrics.cosine", {
              value: formatConfidencePct(item.cosine_similarity),
            })}
          </span>
          {updatedRelative && (
            <time
              dateTime={item.updated_at}
              title={updatedExact}
              className="tabular-nums"
            >
              {updatedRelative}
            </time>
          )}
        </div>
      </div>

      {/* Degraded fallback warning */}
      {item.degraded_fallback && (
        <div className="px-4 pt-3.5">
          <Alert className="border-amber-500/40 bg-amber-500/10 text-amber-900 dark:text-amber-200 [&>svg]:text-amber-600 dark:[&>svg]:text-amber-400">
            <TriangleAlert className="size-4" />
            <AlertTitle>{t("degraded.title")}</AlertTitle>
            <AlertDescription>{t("degraded.description")}</AlertDescription>
          </Alert>
        </div>
      )}

      {/* Side-by-side comparison */}
      <div className="grid grid-cols-1 gap-3 p-4 lg:grid-cols-2">
        <ConflictSidePanel
          side={item.side_a}
          role="a"
          status={item.status}
          locale={locale}
        />
        <ConflictSidePanel
          side={item.side_b}
          role="b"
          status={item.status}
          locale={locale}
        />
      </div>

      {/* Rationale & scope summary */}
      {(item.reason ||
        item.scope_difference_summary ||
        effectiveValidToFormatted ||
        item.resolved_by ||
        resolvedAtFormatted) && (
        <div className="space-y-1.5 border-t bg-muted/15 px-4 py-3 text-xs">
          {item.reason && (
            <p className="leading-relaxed text-foreground/90">
              <span className="mr-1.5 font-medium text-muted-foreground">
                {t("details.reason")}:
              </span>
              {item.reason}
            </p>
          )}
          {item.scope_difference_summary && (
            <p className="leading-relaxed text-foreground/90">
              <span className="mr-1.5 font-medium text-muted-foreground">
                {t("details.scopeDifference")}:
              </span>
              {item.scope_difference_summary}
            </p>
          )}
          {(effectiveValidToFormatted ||
            item.resolved_by ||
            resolvedAtFormatted) && (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 pt-0.5 font-mono text-[11px] tabular-nums text-muted-foreground">
              {effectiveValidToFormatted && (
                <span>
                  {t("details.effectiveValidTo", {
                    date: effectiveValidToFormatted,
                  })}
                </span>
              )}
              {item.resolved_by && (
                <span>{t("details.resolvedBy", { actor: item.resolved_by })}</span>
              )}
              {resolvedAtFormatted && (
                <span>{t("details.resolvedAt", { at: resolvedAtFormatted })}</span>
              )}
            </div>
          )}
        </div>
      )}

      {/* HITL Decision bar */}
      <div className="border-t px-4 py-3">
        {archived ? (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Archive className="size-3.5 shrink-0" />
            <span>{t("actions.archivedNotice")}</span>
          </div>
        ) : (
          <div className="flex flex-col gap-2.5 lg:flex-row lg:items-center">
            <Input
              value={reasonDraft}
              onChange={(event) => onReasonChange(item.id, event.target.value)}
              maxLength={MAX_REASON_LENGTH}
              disabled={isMutating}
              placeholder={t("actions.reasonPlaceholder")}
              aria-label={t("actions.reasonLabel")}
              className="h-8 flex-1 text-xs"
            />
            <div className="flex flex-wrap items-center gap-2">
              {resolvable && (
                <>
                  <Button
                    type="button"
                    size="sm"
                    disabled={isMutating}
                    onClick={() => onResolve(item.id, "WINNER_B")}
                  >
                    {pendingAction === "WINNER_B" ? (
                      <Spinner className="size-3.5" />
                    ) : (
                      <Check className="size-3.5" />
                    )}
                    {t("actions.keepSideB")}
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={isMutating}
                    onClick={() => onResolve(item.id, "WINNER_A")}
                  >
                    {pendingAction === "WINNER_A" ? (
                      <Spinner className="size-3.5" />
                    ) : (
                      <Check className="size-3.5" />
                    )}
                    {t("actions.keepSideA")}
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    disabled={isMutating}
                    onClick={() => onResolve(item.id, "KEEP_BOTH_SCOPED")}
                  >
                    {pendingAction === "KEEP_BOTH_SCOPED" ? (
                      <Spinner className="size-3.5" />
                    ) : (
                      <Split className="size-3.5" />
                    )}
                    {t("actions.keepBothScoped")}
                  </Button>
                </>
              )}
              {reopenable && (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={isMutating}
                  onClick={() => onReopen(item.id)}
                  className="border-amber-500/40 text-amber-800 hover:bg-amber-500/10 dark:text-amber-300"
                >
                  {pendingAction === "REOPEN" ? (
                    <Spinner className="size-3.5" />
                  ) : (
                    <RotateCcw className="size-3.5" />
                  )}
                  {t("actions.reopen")}
                </Button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Collapsible Audit Trail */}
      <div className="border-t bg-muted/10 px-4 py-2">
        <button
          type="button"
          aria-expanded={auditOpen}
          onClick={() => setAuditOpen((open) => !open)}
          className="inline-flex items-center gap-1.5 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
        >
          <History className="size-3.5" />
          <span className="tabular-nums">
            {t("audit.toggle", { count: item.audit_history.length })}
          </span>
          {auditOpen ? (
            <ChevronUp className="size-3.5" />
          ) : (
            <ChevronDown className="size-3.5" />
          )}
        </button>

        {auditOpen && (
          <div className="mt-2.5 pb-1">
            {item.audit_history.length === 0 ? (
              <p className="text-xs text-muted-foreground">{t("audit.empty")}</p>
            ) : (
              <ol className="space-y-2.5 border-l border-border pl-4">
                {item.audit_history.map((entry, idx) => (
                  <li
                    key={`${entry.at}-${entry.action}-${idx}`}
                    className="relative text-xs"
                  >
                    <span className="absolute -left-[21px] top-1.5 size-2 rounded-full border border-border bg-background" />
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="rounded border border-border bg-background px-1.5 py-0.5 font-mono text-[11px] font-medium text-foreground">
                        {entry.action}
                      </span>
                      <span className="text-muted-foreground">
                        {entry.from_status
                          ? t("audit.transition", {
                              from: formatAuditStatus(entry.from_status),
                              to: formatAuditStatus(entry.to_status),
                            })
                          : formatAuditStatus(entry.to_status)}
                      </span>
                      <span className="font-mono text-[11px] text-muted-foreground">
                        {t("audit.actor", { actor: entry.actor })}
                      </span>
                      <time
                        dateTime={entry.at}
                        className="font-mono text-[11px] tabular-nums text-muted-foreground"
                      >
                        {formatDateTime(entry.at, timezone, locale)}
                      </time>
                    </div>
                    {entry.reason && (
                      <p className="mt-1 whitespace-pre-wrap break-words text-foreground/85">
                        {entry.reason}
                      </p>
                    )}
                  </li>
                ))}
              </ol>
            )}
          </div>
        )}
      </div>
    </article>
  );
}

export interface ConflictInboxProps {
  initialSourceId?: string | null;
  initialDocumentId?: string;
  timezone?: string;
}

export function ConflictInbox({
  initialSourceId = null,
  initialDocumentId = "",
  timezone = DEFAULT_TIME_ZONE,
}: ConflictInboxProps) {
  const t = useTranslations("ConflictInbox");
  const tKnowledge = useTranslations("Knowledge");
  const locale = useLocale();

  const [tab, setTab] = React.useState<ConflictFilterTab>("pending");
  const [subtypeFilter, setSubtypeFilter] = React.useState<
    ConflictSubtype | "ALL"
  >("ALL");
  const [sourceId, setSourceId] = React.useState<string | null>(
    initialSourceId?.trim() || null,
  );
  const [docInput, setDocInput] = React.useState(
    initialDocumentId?.trim() || "",
  );
  const [appliedDocumentId, setAppliedDocumentId] = React.useState(
    initialDocumentId?.trim() || "",
  );
  const [includeInactive, setIncludeInactive] = React.useState(false);
  const [offset, setOffset] = React.useState(0);

  const [items, setItems] = React.useState<ConflictItem[] | null>(null);
  const [total, setTotal] = React.useState(0);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);

  const [reasonDrafts, setReasonDrafts] = React.useState<
    Record<string, string>
  >({});
  const [pendingActions, setPendingActions] = React.useState<
    Record<string, PendingMutation | undefined>
  >({});

  const requestSeqRef = React.useRef(0);

  React.useEffect(() => {
    setSourceId(initialSourceId?.trim() || null);
    setOffset(0);
  }, [initialSourceId]);

  React.useEffect(() => {
    const normalized = initialDocumentId?.trim() || "";
    setDocInput(normalized);
    setAppliedDocumentId(normalized);
    setOffset(0);
  }, [initialDocumentId]);

  React.useEffect(() => {
    const trimmed = docInput.trim();
    if (trimmed === appliedDocumentId) return;
    const handle = window.setTimeout(() => {
      setAppliedDocumentId(trimmed);
      setOffset(0);
    }, 300);
    return () => window.clearTimeout(handle);
  }, [appliedDocumentId, docInput]);

  const fetchConflicts = React.useCallback(
    async (overrideOffset?: number) => {
      const targetOffset = overrideOffset ?? offset;
      const seq = ++requestSeqRef.current;
      setLoading(true);
      setError(null);

      const statuses = tabToStatuses(tab);
      try {
        const res = await api.listConflicts({
          source_id: sourceId ?? undefined,
          status: statuses,
          conflict_subtype:
            subtypeFilter === "ALL" ? undefined : [subtypeFilter],
          document_id: appliedDocumentId || undefined,
          include_inactive: includeInactive || undefined,
          limit: PAGE_LIMIT,
          offset: targetOffset,
        });
        if (seq !== requestSeqRef.current) return;
        if (targetOffset > 0 && res.items.length === 0 && res.total > 0) {
          const prevOffset = Math.max(0, targetOffset - PAGE_LIMIT);
          setOffset(prevOffset);
          return;
        }
        setItems(res.items);
        setTotal(res.total);
      } catch (err) {
        if (seq !== requestSeqRef.current) return;
        if (err instanceof ApiError && err.code === "aborted") return;
        const message =
          err instanceof ApiError
            ? err.message
            : err instanceof Error && err.message
              ? err.message
              : t("errors.loadFailed");
        setError(message);
        setItems((current) => current ?? []);
      } finally {
        if (seq === requestSeqRef.current) {
          setLoading(false);
        }
      }
    },
    [appliedDocumentId, includeInactive, offset, sourceId, subtypeFilter, t, tab],
  );

  React.useEffect(() => {
    void fetchConflicts();
  }, [fetchConflicts]);

  const handleTabChange = (value: string) => {
    if (
      value === "pending" ||
      value === "active" ||
      value === "scoped" ||
      value === "all"
    ) {
      setTab(value);
      setOffset(0);
    }
  };

  const handleSubtypeChange = (value: string) => {
    if (value === "ALL") {
      setSubtypeFilter("ALL");
    } else if (CONFLICT_SUBTYPES.includes(value as ConflictSubtype)) {
      setSubtypeFilter(value as ConflictSubtype);
    }
    setOffset(0);
  };

  const handleDocumentSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    const trimmed = docInput.trim();
    setAppliedDocumentId(trimmed);
    setOffset(0);
  };

  const clearDocumentFilter = () => {
    setDocInput("");
    setAppliedDocumentId("");
    setOffset(0);
  };

  const clearSourceFilter = () => {
    setSourceId(null);
    setOffset(0);
  };

  const resetAllFilters = () => {
    setTab("all");
    setSubtypeFilter("ALL");
    setSourceId(null);
    setDocInput("");
    setAppliedDocumentId("");
    setIncludeInactive(false);
    setOffset(0);
  };

  const handleReasonChange = React.useCallback((id: string, value: string) => {
    setReasonDrafts((prev) => ({ ...prev, [id]: value }));
  }, []);

  const handleResolve = React.useCallback(
    async (id: string, action: ConflictResolveAction) => {
      if (pendingActions[id]) return;
      const trimmedReason = (reasonDrafts[id] ?? "").trim();
      setPendingActions((prev) => ({ ...prev, [id]: action }));
      try {
        await api.resolveConflict(
          id,
          trimmedReason ? { action, reason: trimmedReason } : { action },
        );
        setReasonDrafts((prev) => {
          const next = { ...prev };
          delete next[id];
          return next;
        });
        if (action === "WINNER_B") toast.success(t("toasts.resolvedWinnerB"));
        else if (action === "WINNER_A") toast.success(t("toasts.resolvedWinnerA"));
        else toast.success(t("toasts.resolvedKeepBoth"));
        await fetchConflicts();
      } catch (err) {
        toast.error(
          err instanceof ApiError ? err.message : t("toasts.actionError"),
        );
      } finally {
        setPendingActions((prev) => {
          const next = { ...prev };
          delete next[id];
          return next;
        });
      }
    },
    [fetchConflicts, pendingActions, reasonDrafts, t],
  );

  const handleReopen = React.useCallback(
    async (id: string) => {
      if (pendingActions[id]) return;
      const trimmedReason = (reasonDrafts[id] ?? "").trim();
      setPendingActions((prev) => ({ ...prev, [id]: "REOPEN" }));
      try {
        await api.reopenConflict(
          id,
          trimmedReason ? { reason: trimmedReason } : undefined,
        );
        setReasonDrafts((prev) => {
          const next = { ...prev };
          delete next[id];
          return next;
        });
        toast.success(t("toasts.reopened"));
        await fetchConflicts();
      } catch (err) {
        toast.error(
          err instanceof ApiError ? err.message : t("toasts.actionError"),
        );
      } finally {
        setPendingActions((prev) => {
          const next = { ...prev };
          delete next[id];
          return next;
        });
      }
    },
    [fetchConflicts, pendingActions, reasonDrafts, t],
  );

  const hasFilters =
    Boolean(sourceId) ||
    Boolean(appliedDocumentId) ||
    subtypeFilter !== "ALL" ||
    tab !== "all";

  const currentItems = items ?? [];
  const startIndex = total === 0 || currentItems.length === 0 ? 0 : offset + 1;
  const endIndex =
    total === 0 || currentItems.length === 0
      ? 0
      : Math.min(total, offset + currentItems.length);
  const currentPage = Math.floor(offset / PAGE_LIMIT) + 1;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_LIMIT));
  const hasPrev = offset > 0;
  const hasNext = offset + currentItems.length < total;

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-5 p-4 md:p-6">
      <div className="space-y-2">
        <Link
          href="/knowledge"
          className="inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" />
          {tKnowledge("allSources")}
        </Link>

        <PageHeader
          title={t("title")}
          description={t("description")}
          actions={
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={loading}
              onClick={() => void fetchConflicts()}
            >
              <RotateCw className={cn("size-3.5", loading && "animate-spin")} />
              {t("refresh")}
            </Button>
          }
        />
      </div>

      {/* Filter & Context Bar */}
      <div className="flex flex-col gap-3 rounded-lg border bg-card p-3.5 shadow-soft">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <Tabs value={tab} onValueChange={handleTabChange}>
            <TabsList aria-label={t("tabs.aria")}>
              <TabsTrigger value="pending">{t("tabs.pending")}</TabsTrigger>
              <TabsTrigger value="active">{t("tabs.active")}</TabsTrigger>
              <TabsTrigger value="scoped">{t("tabs.scoped")}</TabsTrigger>
              <TabsTrigger value="all">{t("tabs.all")}</TabsTrigger>
            </TabsList>
          </Tabs>

          <div className="flex items-center gap-2">
            <Switch
              id="conflict-include-inactive"
              checked={includeInactive}
              onCheckedChange={(checked) => {
                setIncludeInactive(checked);
                setOffset(0);
              }}
            />
            <label
              htmlFor="conflict-include-inactive"
              className="cursor-pointer select-none text-xs font-medium text-muted-foreground"
            >
              {t("filters.includeInactive")}
            </label>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2.5">
          <Select value={subtypeFilter} onValueChange={handleSubtypeChange}>
            <SelectTrigger
              aria-label={t("filters.subtypeAria")}
              className="h-9 w-full sm:w-56"
            >
              <SelectValue placeholder={t("filters.allSubtypes")} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="ALL">{t("filters.allSubtypes")}</SelectItem>
              {CONFLICT_SUBTYPES.map((subtype) => (
                <SelectItem key={subtype} value={subtype}>
                  {t(`subtypes.${subtype}`)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <form
            onSubmit={handleDocumentSubmit}
            className="relative flex min-w-[220px] flex-1 items-center"
          >
            <Search className="pointer-events-none absolute left-2.5 size-3.5 text-muted-foreground" />
            <Input
              value={docInput}
              onChange={(event) => setDocInput(event.target.value)}
              placeholder={t("filters.documentPlaceholder")}
              aria-label={t("filters.documentAria")}
              className="h-9 pl-8 pr-8 text-xs"
            />
            {docInput && (
              <button
                type="button"
                onClick={clearDocumentFilter}
                aria-label={t("filters.clearDocument")}
                className="absolute right-2 rounded p-0.5 text-muted-foreground transition-colors hover:text-foreground"
              >
                <X className="size-3.5" />
              </button>
            )}
          </form>

          {sourceId && (
            <span
              data-testid="active-source-filter"
              className="inline-flex items-center gap-1.5 rounded-full border border-border bg-muted/60 py-1 pl-2.5 pr-1.5 font-mono text-xs text-foreground"
            >
              <span>{t("filters.sourceChip", { sourceId })}</span>
              <button
                type="button"
                onClick={clearSourceFilter}
                aria-label={t("filters.clearSource")}
                className="rounded-full p-0.5 text-muted-foreground transition-colors hover:bg-background hover:text-foreground"
              >
                <X className="size-3" />
              </button>
            </span>
          )}
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-2.5 text-xs tabular-nums text-muted-foreground">
          <span>
            {total > 0 && currentItems.length > 0
              ? t("summary.showing", {
                  start: startIndex,
                  end: endIndex,
                  total,
                })
              : t("summary.zero")}
          </span>
          {total > 0 && (
            <span className="font-mono text-[11px]">
              {t("summary.page", { page: currentPage, totalPages })}
            </span>
          )}
        </div>
      </div>

      {/* Error Banner */}
      {error && (
        <Alert variant="destructive" className="flex items-center justify-between gap-3">
          <div className="flex items-start gap-2.5">
            <TriangleAlert className="mt-0.5 size-4 shrink-0" />
            <div>
              <AlertTitle>{t("errors.loadFailed")}</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </div>
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => void fetchConflicts()}
            className="shrink-0"
          >
            {t("retry")}
          </Button>
        </Alert>
      )}

      {/* Body */}
      {loading && items === null ? (
        <div className="space-y-4">
          {Array.from({ length: 2 }).map((_, index) => (
            <div
              key={index}
              className="space-y-3 rounded-lg border bg-card p-4 shadow-soft"
            >
              <Skeleton className="h-6 w-2/3" />
              <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                <Skeleton className="h-40 rounded-md" />
                <Skeleton className="h-40 rounded-md" />
              </div>
              <Skeleton className="h-9 w-full" />
            </div>
          ))}
        </div>
      ) : currentItems.length === 0 && !error ? (
        <EmptyState
          icon={Scale}
          title={t("empty.title")}
          description={
            hasFilters ? t("empty.filteredDescription") : t("empty.description")
          }
          action={
            hasFilters ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={resetAllFilters}
              >
                {t("filters.resetAll")}
              </Button>
            ) : undefined
          }
        />
      ) : (
        <div className="space-y-4">
          {currentItems.map((item) => (
            <ConflictCard
              key={item.id}
              item={item}
              locale={locale}
              timezone={timezone}
              reasonDraft={reasonDrafts[item.id] ?? ""}
              pendingAction={pendingActions[item.id]}
              onReasonChange={handleReasonChange}
              onResolve={(id, action) => void handleResolve(id, action)}
              onReopen={(id) => void handleReopen(id)}
            />
          ))}
        </div>
      )}

      {/* Pagination Bar */}
      {(total > PAGE_LIMIT || offset > 0) && (
        <div className="flex items-center justify-between gap-3 border-t pt-4">
          <span className="font-mono text-xs tabular-nums text-muted-foreground">
            {t("summary.page", { page: currentPage, totalPages })}
          </span>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!hasPrev || loading}
              onClick={() => setOffset((prev) => Math.max(0, prev - PAGE_LIMIT))}
            >
              <ChevronLeft className="size-3.5" />
              {t("pagination.previous")}
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!hasNext || loading}
              onClick={() => setOffset((prev) => prev + PAGE_LIMIT)}
            >
              {t("pagination.next")}
              <ChevronRight className="size-3.5" />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
