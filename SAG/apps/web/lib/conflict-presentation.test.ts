import { describe, expect, it } from "vitest";

import {
  CONFLICT_SUBTYPES,
  auditEntryTimestamp,
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
} from "./conflict-presentation";
import type { ConflictResolutionStatus } from "./types";

describe("conflict presentation helpers", () => {
  it("maps filter tabs to exact API status filters", () => {
    expect(tabToStatuses("pending")).toEqual([
      "PENDING_REVIEW",
      "NEEDS_REVALIDATION",
    ]);
    expect(tabToStatuses("active")).toEqual(["ACTIVE"]);
    expect(tabToStatuses("scoped")).toEqual(["DISMISSED_SCOPED"]);
    expect(tabToStatuses("all")).toBeUndefined();
  });

  it("enforces resolve, reopen, and archived capability rules for every status", () => {
    const cases: Array<{
      status: ConflictResolutionStatus;
      resolve: boolean;
      reopen: boolean;
      archived: boolean;
    }> = [
      { status: "PENDING_REVIEW", resolve: true, reopen: false, archived: false },
      {
        status: "NEEDS_REVALIDATION",
        resolve: true,
        reopen: false,
        archived: false,
      },
      { status: "ACTIVE", resolve: true, reopen: true, archived: false },
      {
        status: "DISMISSED_SCOPED",
        resolve: true,
        reopen: true,
        archived: false,
      },
      { status: "REVOKED_WINNER", resolve: false, reopen: false, archived: true },
      {
        status: "SUPERSEDED_BY_UPDATE",
        resolve: false,
        reopen: false,
        archived: true,
      },
    ];

    for (const c of cases) {
      expect(canResolveConflict(c.status)).toBe(c.resolve);
      expect(canReopenConflict(c.status)).toBe(c.reopen);
      expect(isArchivedConflict(c.status)).toBe(c.archived);
      expect(conflictStatusTone(c.status).length).toBeGreaterThan(0);
    }
  });

  it("returns semantic tone classes for all conflict subtypes", () => {
    for (const subtype of CONFLICT_SUBTYPES) {
      expect(conflictSubtypeTone(subtype).length).toBeGreaterThan(0);
    }
  });

  it("resolves valid authority tiers and rejects out-of-range or non-integer values", () => {
    expect(authorityTierMeta(1)).toMatchObject({
      tier: 1,
      code: "T1",
      i18nKey: "tier1",
    });
    expect(authorityTierMeta(2)).toMatchObject({
      tier: 2,
      code: "T2",
      i18nKey: "tier2",
    });
    expect(authorityTierMeta(3)).toMatchObject({
      tier: 3,
      code: "T3",
      i18nKey: "tier3",
    });
    expect(authorityTierMeta(4)).toMatchObject({
      tier: 4,
      code: "T4",
      i18nKey: "tier4",
    });

    expect(authorityTierMeta(null)).toBeNull();
    expect(authorityTierMeta(undefined)).toBeNull();
    expect(authorityTierMeta(0)).toBeNull();
    expect(authorityTierMeta(5)).toBeNull();
    expect(authorityTierMeta(1.5)).toBeNull();
    expect(authorityTierMeta(Number.NaN)).toBeNull();
  });

  it("formats valid conflict dates defensively across locales and rejects invalid dates", () => {
    const enDate = formatConflictDate("2026-01-15", "en-US");
    expect(enDate).toContain("2026");
    expect(enDate).toContain("15");

    const isoTimestamp = formatConflictDate("2026-03-01T00:00:00Z", "en-US");
    expect(isoTimestamp).toContain("2026");

    const fallbackLocale = formatConflictDate(
      "2026-06-10T12:00:00Z",
      "not_a_valid_locale_tag___",
    );
    expect(fallbackLocale).toContain("2026");

    expect(formatConflictDate(null, "en-US")).toBeNull();
    expect(formatConflictDate(undefined, "en-US")).toBeNull();
    expect(formatConflictDate("   ", "en-US")).toBeNull();
    expect(formatConflictDate("not-a-date", "en-US")).toBeNull();
    expect(formatConflictDate("2026-02-31", "en-US")).toBeNull();
  });

  it("clamps confidence percentages and safely handles NaN/null/Infinity", () => {
    expect(formatConfidencePct(0.924)).toBe("92%");
    expect(formatConfidencePct(0)).toBe("0%");
    expect(formatConfidencePct(1)).toBe("100%");
    expect(formatConfidencePct(-0.4)).toBe("0%");
    expect(formatConfidencePct(1.75)).toBe("100%");
    expect(formatConfidencePct(null)).toBe("—");
    expect(formatConfidencePct(undefined)).toBe("—");
    expect(formatConfidencePct(Number.NaN)).toBe("—");
    expect(formatConfidencePct(Number.POSITIVE_INFINITY)).toBe("—");
  });

  it("formats single, multi-page, reversed, and missing page ranges", () => {
    expect(formatPageRange(4, 4)).toBe("p. 4");
    expect(formatPageRange(4, 6)).toBe("p. 4–6");
    expect(formatPageRange(8, 5)).toBe("p. 5–8");
    expect(formatPageRange(3, null)).toBe("p. 3");
    expect(formatPageRange(null, 9)).toBe("p. 9");
    expect(formatPageRange(null, null)).toBeNull();
    expect(formatPageRange(0, 0)).toBeNull();
    expect(formatPageRange(-2, Number.NaN)).toBeNull();
  });

  it("compares authority precedence correctly between Side A and Side B", () => {
    expect(
      compareAuthorityPrecedence({ authority_tier: 2 }, { authority_tier: 1 }),
    ).toBe("b_higher");
    expect(
      compareAuthorityPrecedence({ authority_tier: 1 }, { authority_tier: 3 }),
    ).toBe("a_higher");
    expect(
      compareAuthorityPrecedence({ authority_tier: 2 }, { authority_tier: 2 }),
    ).toBe("equal");
    expect(
      compareAuthorityPrecedence({ authority_tier: null }, { authority_tier: 1 }),
    ).toBe("incomplete");
    expect(
      compareAuthorityPrecedence({ authority_tier: 1 }, { authority_tier: 99 }),
    ).toBe("incomplete");
  });

  it("extracts audit entry timestamp preferring at over timestamp and returning null when absent", () => {
    expect(
      auditEntryTimestamp({
        at: "2026-11-26T10:05:00+00:00",
        timestamp: "2026-11-26T10:00:00+00:00",
      }),
    ).toBe("2026-11-26T10:05:00+00:00");
    expect(
      auditEntryTimestamp({
        timestamp: "  2026-11-26T10:00:00+00:00  ",
      }),
    ).toBe("2026-11-26T10:00:00+00:00");
    expect(
      auditEntryTimestamp({
        at: "   ",
        timestamp: "2026-11-26T10:00:00+00:00",
      }),
    ).toBe("2026-11-26T10:00:00+00:00");
    expect(auditEntryTimestamp({ at: null, timestamp: null })).toBeNull();
    expect(auditEntryTimestamp({ at: "", timestamp: "   " })).toBeNull();
    expect(auditEntryTimestamp({})).toBeNull();
  });
});
