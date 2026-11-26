import { parseUtcDate } from "@/lib/format";
import type {
  ConflictResolutionStatus,
  ConflictSide,
  ConflictSubtype,
} from "@/lib/types";

export type ConflictFilterTab = "pending" | "active" | "scoped" | "all";

export type AuthorityTierKey = "tier1" | "tier2" | "tier3" | "tier4";

export type AuthorityPrecedence =
  | "b_higher"
  | "a_higher"
  | "equal"
  | "incomplete";

export interface AuthorityTierMeta {
  tier: 1 | 2 | 3 | 4;
  code: "T1" | "T2" | "T3" | "T4";
  i18nKey: AuthorityTierKey;
  toneClass: string;
}

export const CONFLICT_SUBTYPES: readonly ConflictSubtype[] = [
  "DIRECT_SUPERSEDES",
  "HARD_CONTRADICTION",
  "SCOPED_EXCEPTION",
  "DIFFERENT_PERSPECTIVE",
  "SUPPORTS_OR_EQUIVALENT",
];

const RESOLVABLE_STATUSES: ReadonlySet<ConflictResolutionStatus> = new Set([
  "PENDING_REVIEW",
  "NEEDS_REVALIDATION",
  "ACTIVE",
  "DISMISSED_SCOPED",
]);

const REOPENABLE_STATUSES: ReadonlySet<ConflictResolutionStatus> = new Set([
  "ACTIVE",
  "DISMISSED_SCOPED",
]);

const AUTHORITY_TIERS: Record<1 | 2 | 3 | 4, AuthorityTierMeta> = {
  1: {
    tier: 1,
    code: "T1",
    i18nKey: "tier1",
    toneClass:
      "border-emerald-500/30 bg-emerald-500/10 text-emerald-800 dark:text-emerald-300",
  },
  2: {
    tier: 2,
    code: "T2",
    i18nKey: "tier2",
    toneClass:
      "border-sky-500/30 bg-sky-500/10 text-sky-800 dark:text-sky-300",
  },
  3: {
    tier: 3,
    code: "T3",
    i18nKey: "tier3",
    toneClass: "border-border bg-muted/70 text-foreground/80",
  },
  4: {
    tier: 4,
    code: "T4",
    i18nKey: "tier4",
    toneClass:
      "border-amber-500/30 bg-amber-500/10 text-amber-800 dark:text-amber-300",
  },
};

export function tabToStatuses(
  tab: ConflictFilterTab,
): ConflictResolutionStatus[] | undefined {
  switch (tab) {
    case "pending":
      return ["PENDING_REVIEW", "NEEDS_REVALIDATION"];
    case "active":
      return ["ACTIVE"];
    case "scoped":
      return ["DISMISSED_SCOPED"];
    case "all":
      return undefined;
  }
}

export function canResolveConflict(status: ConflictResolutionStatus): boolean {
  return RESOLVABLE_STATUSES.has(status);
}

export function canReopenConflict(status: ConflictResolutionStatus): boolean {
  return REOPENABLE_STATUSES.has(status);
}

export function isArchivedConflict(status: ConflictResolutionStatus): boolean {
  return !canResolveConflict(status) && !canReopenConflict(status);
}

export function authorityTierMeta(
  tier: number | null | undefined,
): AuthorityTierMeta | null {
  if (tier == null || !Number.isInteger(tier)) return null;
  if (tier === 1 || tier === 2 || tier === 3 || tier === 4) {
    return AUTHORITY_TIERS[tier];
  }
  return null;
}

function parseStrictIsoDate(raw: string): Date | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const dateOnlyMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(trimmed);
  if (dateOnlyMatch) {
    const year = Number(dateOnlyMatch[1]);
    const month = Number(dateOnlyMatch[2]);
    const day = Number(dateOnlyMatch[3]);
    const dt = new Date(Date.UTC(year, month - 1, day));
    if (
      dt.getUTCFullYear() !== year ||
      dt.getUTCMonth() !== month - 1 ||
      dt.getUTCDate() !== day
    ) {
      return null;
    }
    return dt;
  }
  const dt = parseUtcDate(trimmed);
  return Number.isNaN(dt.getTime()) ? null : dt;
}

export function formatConflictDate(
  iso: string | null | undefined,
  locale = "en-US",
): string | null {
  if (!iso) return null;
  const parsed = parseStrictIsoDate(iso);
  if (!parsed) return null;
  const options: Intl.DateTimeFormatOptions = {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  };
  try {
    return new Intl.DateTimeFormat(locale, options).format(parsed);
  } catch {
    return new Intl.DateTimeFormat("en-US", options).format(parsed);
  }
}

export function formatConfidencePct(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const clamped = Math.min(1, Math.max(0, value));
  return `${Math.round(clamped * 100)}%`;
}

export function formatPageRange(
  pageFrom: number | null | undefined,
  pageTo: number | null | undefined,
): string | null {
  const from =
    pageFrom != null && Number.isInteger(pageFrom) && pageFrom > 0
      ? pageFrom
      : null;
  const to =
    pageTo != null && Number.isInteger(pageTo) && pageTo > 0 ? pageTo : null;
  if (from === null && to === null) return null;
  if (from === null) return `p. ${to}`;
  if (to === null || from === to) return `p. ${from}`;
  const start = Math.min(from, to);
  const end = Math.max(from, to);
  return `p. ${start}\u2013${end}`;
}

export function conflictStatusTone(status: ConflictResolutionStatus): string {
  switch (status) {
    case "PENDING_REVIEW":
      return "border-amber-500/35 bg-amber-500/12 text-amber-800 dark:text-amber-300";
    case "NEEDS_REVALIDATION":
      return "border-orange-500/35 bg-orange-500/12 text-orange-800 dark:text-orange-300";
    case "ACTIVE":
      return "border-emerald-500/35 bg-emerald-500/12 text-emerald-800 dark:text-emerald-300";
    case "DISMISSED_SCOPED":
      return "border-sky-500/35 bg-sky-500/12 text-sky-800 dark:text-sky-300";
    case "REVOKED_WINNER":
      return "border-destructive/35 bg-destructive/10 text-destructive";
    case "SUPERSEDED_BY_UPDATE":
      return "border-border bg-muted text-muted-foreground";
  }
}

export function conflictSubtypeTone(subtype: ConflictSubtype): string {
  switch (subtype) {
    case "DIRECT_SUPERSEDES":
      return "border-zinc-500/30 bg-zinc-500/10 text-zinc-800 dark:text-zinc-200";
    case "HARD_CONTRADICTION":
      return "border-destructive/35 bg-destructive/10 text-destructive";
    case "SCOPED_EXCEPTION":
      return "border-sky-500/35 bg-sky-500/10 text-sky-800 dark:text-sky-300";
    case "DIFFERENT_PERSPECTIVE":
      return "border-amber-500/35 bg-amber-500/10 text-amber-800 dark:text-amber-300";
    case "SUPPORTS_OR_EQUIVALENT":
      return "border-emerald-500/35 bg-emerald-500/10 text-emerald-800 dark:text-emerald-300";
  }
}

export function compareAuthorityPrecedence(
  sideA: Pick<ConflictSide, "authority_tier">,
  sideB: Pick<ConflictSide, "authority_tier">,
): AuthorityPrecedence {
  const tierA = authorityTierMeta(sideA.authority_tier);
  const tierB = authorityTierMeta(sideB.authority_tier);
  if (!tierA || !tierB) return "incomplete";
  if (tierB.tier < tierA.tier) return "b_higher";
  if (tierA.tier < tierB.tier) return "a_higher";
  return "equal";
}
