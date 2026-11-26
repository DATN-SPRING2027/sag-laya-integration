// @vitest-environment jsdom
import * as React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import enMessages from "@/messages/en-US.json";
import { api, ApiError } from "@/lib/api";
import type {
  ConflictActionResponse,
  ConflictItem,
  ConflictListParams,
  ConflictListResponse,
} from "@/lib/types";
import { ConflictInbox } from "@/components/features/conflict-inbox";

const mockRouterReplace = vi.fn();
let mockSearchParams = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockRouterReplace }),
  usePathname: () => "/knowledge/conflicts",
  useSearchParams: () => mockSearchParams,
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    ...props
  }: {
    href: string;
    children: React.ReactNode;
  } & React.AnchorHTMLAttributes<HTMLAnchorElement>) =>
    React.createElement("a", { href, ...props }, children),
}));

(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

function buildConflict(overrides: Partial<ConflictItem> = {}): ConflictItem {
  return {
    id: "res-001",
    tenant_id: "tenant-1",
    project_id: "proj-hr",
    security_partition_id: "part-hr-general",
    winner_security_partition_id: "part-hr-general",
    pair_key: "abc123def456",
    conflict_subtype: "DIRECT_SUPERSEDES",
    status: "PENDING_REVIEW",
    resolution_mode: "UNRESOLVED",
    confidence: 0.92,
    cosine_similarity: 0.88,
    effective_valid_to: null,
    scope_difference_summary: "Applies to full-time employees hired after 2025.",
    reason: "Annual leave quota increased from 12 to 15 working days.",
    degraded_fallback: false,
    graph_edge_id: null,
    resolved_by: null,
    resolved_at: null,
    created_at: "2026-01-10T09:00:00Z",
    updated_at: "2026-01-12T10:30:00Z",
    audit_history: [
      {
        action: "DETECTED",
        actor: "system",
        reason: "Detected high semantic overlap with conflicting leave count.",
        from_status: null,
        to_status: "PENDING_REVIEW",
        at: "2026-01-10T09:00:00Z",
      },
      {
        action: "FLAGGED_FOR_HITL",
        actor: "policy-engine",
        reason: "Requires human sign-off before enforcing precedence.",
        from_status: "PENDING_REVIEW",
        to_status: "PENDING_REVIEW",
        at: "2026-01-12T10:30:00Z",
      },
    ],
    side_a: {
      document_id: "doc-leave-2024",
      document_name: "HR_Leave_Policy_2024.pdf",
      version_id: "ver-2024-1",
      version_no: 1,
      unit_id: "unit-a1",
      section_path: "Section 3 > Annual Leave",
      page_from: 4,
      page_to: 4,
      text_snapshot: "Nhân viên chính thức được hưởng 12 ngày phép năm.",
      authority_tier: 2,
      effective_from: "2024-01-01T00:00:00Z",
      security_partition_id: "part-hr-general",
    },
    side_b: {
      document_id: "doc-leave-2026",
      document_name: "Board_Resolution_Leave_2026.pdf",
      version_id: "ver-2026-2",
      version_no: 2,
      unit_id: "unit-b1",
      section_path: "Article 2 > Leave Entitlement",
      page_from: 2,
      page_to: 3,
      text_snapshot: "Nhân viên chính thức được hưởng 15 ngày phép năm từ 2026.",
      authority_tier: 1,
      effective_from: "2026-01-01T00:00:00Z",
      security_partition_id: "part-hr-general",
    },
    ...overrides,
  };
}

function buildActionResponse(
  overrides: Partial<ConflictActionResponse> = {},
): ConflictActionResponse {
  return {
    id: "res-001",
    tenant_id: "tenant-1",
    project_id: "proj-hr",
    security_partition_id: "part-hr-general",
    winner_security_partition_id: "part-hr-general",
    pair_key: "abc123def456",
    conflict_subtype: "DIRECT_SUPERSEDES",
    status: "ACTIVE",
    resolution_mode: "MANUAL_HITL",
    winner_document_id: "doc-leave-2026",
    winner_version_id: "ver-2026-2",
    winner_unit_id: "unit-b1",
    loser_document_id: "doc-leave-2024",
    loser_version_id: "ver-2024-1",
    loser_unit_id: "unit-a1",
    effective_valid_to: "2025-12-31T23:59:59Z",
    reason: "Confirmed by HR director",
    degraded_fallback: false,
    graph_edge_id: "edge-1",
    resolved_by: "user-admin",
    resolved_at: "2026-01-13T08:00:00Z",
    created_at: "2026-01-10T09:00:00Z",
    updated_at: "2026-01-13T08:00:00Z",
    audit_history: [],
    ...overrides,
  };
}

async function flushPromises() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function mountInbox(props: React.ComponentProps<typeof ConflictInbox> = {}) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => {
    root.render(
      <NextIntlClientProvider locale="en-US" messages={enMessages}>
        <ConflictInbox timezone="UTC" {...props} />
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

function setInputValue(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    "value",
  )?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function findButtonByText(
  container: HTMLElement,
  text: string,
): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll("button")).find((btn) =>
    btn.textContent?.includes(text),
  );
}

describe("ConflictInbox component", () => {
  let listSpy: ReturnType<typeof vi.spyOn>;
  let resolveSpy: ReturnType<typeof vi.spyOn>;
  let reopenSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    mockRouterReplace.mockReset();
    mockSearchParams = new URLSearchParams();
    listSpy = vi.spyOn(api, "listConflicts");
    resolveSpy = vi.spyOn(api, "resolveConflict");
    reopenSpy = vi.spyOn(api, "reopenConflict");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    document.body.replaceChildren();
  });

  it("loads and renders Side A vs Side B excerpts, authority tiers, dates, metrics, and degraded fallback alert", async () => {
    const item = buildConflict({ degraded_fallback: true });
    const response: ConflictListResponse = {
      items: [item],
      total: 1,
      limit: 20,
      offset: 0,
    };
    listSpy.mockResolvedValueOnce(response);

    const { container, unmount } = mountInbox();
    await flushPromises();

    expect(listSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        status: ["PENDING_REVIEW", "NEEDS_REVALIDATION"],
        limit: 20,
        offset: 0,
      }),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );

    const text = container.textContent ?? "";
    expect(text).toContain("12 ngày");
    expect(text).toContain("15 ngày");
    expect(text).toContain("HR_Leave_Policy_2024.pdf");
    expect(text).toContain("Board_Resolution_Leave_2026.pdf");
    expect(text).toContain("T2 · Policy");
    expect(text).toContain("T1 · Official");
    expect(text).toContain("Side B higher (T1 > T2)");
    expect(text).toContain("Conf 92%");
    expect(text).toContain("Cos 88%");
    expect(text).toContain("2024");
    expect(text).toContain("2026");
    expect(text).toContain("p. 4");
    expect(text).toContain("p. 2–3");
    expect(text).toContain("Heuristic fallback active");

    // Heading hierarchy uses h2 inside cards under page h1, and document filter enforces maxLength=256
    expect(container.querySelectorAll("h2").length).toBeGreaterThanOrEqual(2);
    expect(container.querySelectorAll("h3").length).toBe(0);
    const docInput = container.querySelector<HTMLInputElement>(
      'input[aria-label="Filter by document ID"]',
    );
    expect(docInput?.maxLength).toBe(256);
    unmount();
  });

  it("invokes resolveConflict for WINNER_B, WINNER_A, and KEEP_BOTH_SCOPED with typed rationale and refreshes", async () => {
    const item = buildConflict();
    listSpy.mockResolvedValue({
      items: [item],
      total: 1,
      limit: 20,
      offset: 0,
    });
    resolveSpy.mockResolvedValue(buildActionResponse());

    const { container, unmount } = mountInbox();
    await flushPromises();

    const reasonInput = container.querySelector<HTMLInputElement>(
      'input[aria-label="Arbitration rationale (optional)"]',
    )!;
    expect(reasonInput).not.toBeNull();

    // 1. WINNER_B
    act(() => {
      setInputValue(reasonInput, "Board resolution supersedes 2024 handbook");
    });
    const keepB = findButtonByText(container, "Keep Side B")!;
    await act(async () => {
      keepB.click();
    });
    await flushPromises();
    expect(resolveSpy).toHaveBeenNthCalledWith(1, "res-001", {
      action: "WINNER_B",
      reason: "Board resolution supersedes 2024 handbook",
    });

    // 2. WINNER_A
    act(() => {
      setInputValue(reasonInput, "Retain baseline policy pending legal review");
    });
    const keepA = findButtonByText(container, "Keep Side A")!;
    await act(async () => {
      keepA.click();
    });
    await flushPromises();
    expect(resolveSpy).toHaveBeenNthCalledWith(2, "res-001", {
      action: "WINNER_A",
      reason: "Retain baseline policy pending legal review",
    });

    // 3. KEEP_BOTH_SCOPED
    act(() => {
      setInputValue(reasonInput, "Both apply to different employee cohorts");
    });
    const keepBoth = findButtonByText(container, "Keep Both (Scoped)")!;
    await act(async () => {
      keepBoth.click();
    });
    await flushPromises();
    expect(resolveSpy).toHaveBeenNthCalledWith(3, "res-001", {
      action: "KEEP_BOTH_SCOPED",
      reason: "Both apply to different employee cohorts",
    });

    // Initial load + 3 refreshes
    expect(listSpy).toHaveBeenCalledTimes(4);
    unmount();
  });

  it("switches to Active tab with status=['ACTIVE'] and invokes reopenConflict with rationale", async () => {
    const pendingItem = buildConflict({ id: "res-pending", status: "PENDING_REVIEW" });
    const activeItem = buildConflict({
      id: "res-active",
      status: "ACTIVE",
      resolution_mode: "MANUAL_HITL",
      resolved_by: "alice@company.com",
    });

    listSpy
      .mockResolvedValueOnce({
        items: [pendingItem],
        total: 1,
        limit: 20,
        offset: 0,
      })
      .mockResolvedValueOnce({
        items: [activeItem],
        total: 1,
        limit: 20,
        offset: 0,
      })
      .mockResolvedValueOnce({
        items: [],
        total: 0,
        limit: 20,
        offset: 0,
      });

    reopenSpy.mockResolvedValueOnce(
      buildActionResponse({ id: "res-active", status: "PENDING_REVIEW" }),
    );

    const { container, unmount } = mountInbox();
    await flushPromises();

    const activeTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent?.trim() === "Active",
    ) as HTMLElement;
    expect(activeTab).toBeDefined();

    await act(async () => {
      activeTab.dispatchEvent(
        new MouseEvent("mousedown", { bubbles: true, button: 0 }),
      );
      activeTab.click();
    });
    await flushPromises();

    expect(listSpy).toHaveBeenLastCalledWith(
      expect.objectContaining({
        status: ["ACTIVE"],
      }),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(container.textContent).toContain("Side B · Active winner");
    expect(container.textContent).toContain("Side A · Superseded");

    const reasonInput = container.querySelector<HTMLInputElement>(
      'input[aria-label="Arbitration rationale (optional)"]',
    )!;
    act(() => {
      setInputValue(reasonInput, "New addendum submitted by legal team");
    });

    const reopenBtn = findButtonByText(container, "Reopen for review")!;
    expect(reopenBtn).toBeDefined();
    await act(async () => {
      reopenBtn.click();
    });
    await flushPromises();

    expect(reopenSpy).toHaveBeenCalledWith("res-active", {
      reason: "New addendum submitted by legal team",
    });
    expect(listSpy).toHaveBeenCalledTimes(3);
    unmount();
  });

  it("renders REVOKED_WINNER as read-only archived notice with zero resolve or reopen buttons", async () => {
    const revoked = buildConflict({
      id: "res-revoked",
      status: "REVOKED_WINNER",
    });
    listSpy.mockResolvedValueOnce({
      items: [revoked],
      total: 1,
      limit: 20,
      offset: 0,
    });

    const { container, unmount } = mountInbox();
    await flushPromises();

    expect(container.textContent).toContain("Revoked winner");
    expect(container.textContent).toContain("archived and read-only");
    expect(findButtonByText(container, "Keep Side B")).toBeUndefined();
    expect(findButtonByText(container, "Keep Side A")).toBeUndefined();
    expect(findButtonByText(container, "Keep Both (Scoped)")).toBeUndefined();
    expect(findButtonByText(container, "Reopen for review")).toBeUndefined();
    unmount();
  });

  it("passes source_id from initialSourceId, removes it on clear, and syncs URL query params", async () => {
    mockSearchParams = new URLSearchParams("source=src-hr-99&document=doc-keep");
    listSpy.mockResolvedValue({
      items: [buildConflict()],
      total: 1,
      limit: 20,
      offset: 0,
    });

    const { container, unmount } = mountInbox({
      initialSourceId: "src-hr-99",
      initialDocumentId: "doc-keep",
    });
    await flushPromises();

    expect(listSpy).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({ source_id: "src-hr-99", document_id: "doc-keep" }),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    const chip = container.querySelector('[data-testid="active-source-filter"]');
    expect(chip?.textContent).toContain("Source: src-hr-99");

    const clearBtn = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Clear source filter"]',
    )!;
    expect(clearBtn).not.toBeNull();

    await act(async () => {
      clearBtn.click();
    });
    await flushPromises();

    expect(
      container.querySelector('[data-testid="active-source-filter"]'),
    ).toBeNull();
    expect(mockRouterReplace).toHaveBeenCalledWith(
      "/knowledge/conflicts?document=doc-keep",
      { scroll: false },
    );
    expect(listSpy).toHaveBeenLastCalledWith(
      expect.objectContaining({ source_id: undefined }),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    unmount();
  });

  it("expands collapsible Audit Trail with backend timestamp, normalized at, and missing timestamp without crashing", async () => {
    const auditItem = buildConflict({
      audit_history: [
        {
          action: "DETECTED",
          actor: "system",
          reason: "Backend-native timestamp only.",
          from_status: null,
          to_status: "PENDING_REVIEW",
          timestamp: "2026-11-26T10:00:00+00:00",
        },
        {
          action: "FLAGGED_FOR_HITL",
          actor: "policy-engine",
          reason: "Normalized at field present.",
          from_status: "PENDING_REVIEW",
          to_status: "PENDING_REVIEW",
          at: "2026-11-26T10:05:00+00:00",
        },
        {
          action: "MANUAL_NOTE",
          actor: "auditor",
          reason: "Legacy entry missing timestamp.",
          from_status: "PENDING_REVIEW",
          to_status: "PENDING_REVIEW",
        },
      ],
    });
    listSpy.mockResolvedValueOnce({
      items: [auditItem],
      total: 1,
      limit: 20,
      offset: 0,
    });

    const { container, unmount } = mountInbox();
    await flushPromises();

    expect(container.textContent).not.toContain("FLAGGED_FOR_HITL");

    const auditToggle = findButtonByText(container, "Audit trail (3)")!;
    expect(auditToggle).toBeDefined();
    expect(auditToggle.getAttribute("aria-expanded")).toBe("false");
    expect(auditToggle.id).toBeTruthy();
    const controlsId = auditToggle.getAttribute("aria-controls");
    expect(controlsId).toBeTruthy();

    act(() => {
      auditToggle.click();
    });

    expect(auditToggle.getAttribute("aria-expanded")).toBe("true");
    const auditPanel = container.querySelector(`[id="${controlsId}"]`);
    expect(auditPanel).not.toBeNull();
    expect(container.textContent).toContain("DETECTED");
    expect(container.textContent).toContain("FLAGGED_FOR_HITL");
    expect(container.textContent).toContain("MANUAL_NOTE");
    expect(container.textContent).toContain("Actor: policy-engine");
    expect(container.textContent).toContain("Backend-native timestamp only.");
    expect(container.textContent).toContain("Legacy entry missing timestamp.");

    const timeEls = Array.from(auditPanel?.querySelectorAll("time") ?? []);
    expect(timeEls.map((el) => el.getAttribute("datetime"))).toEqual([
      "2026-11-26T10:00:00+00:00",
      "2026-11-26T10:05:00+00:00",
    ]);
    expect(auditPanel?.textContent).toContain("—");
    unmount();
  });

  it("displays an error banner on API failure and recovers cleanly when Retry is clicked", async () => {
    listSpy
      .mockRejectedValueOnce(
        new ApiError(503, "service_unavailable", "Conflict service temporarily unavailable"),
      )
      .mockResolvedValueOnce({
        items: [buildConflict()],
        total: 1,
        limit: 20,
        offset: 0,
      });

    const { container, unmount } = mountInbox();
    await flushPromises();

    expect(container.textContent).toContain(
      "Conflict service temporarily unavailable",
    );

    const retryBtn = findButtonByText(container, "Retry")!;
    expect(retryBtn).toBeDefined();

    await act(async () => {
      retryBtn.click();
    });
    await flushPromises();

    expect(listSpy).toHaveBeenCalledTimes(2);
    expect(container.textContent).toContain("Board_Resolution_Leave_2026.pdf");
    unmount();
  });

  it("escapes XSS payloads in text_snapshot and reason without injecting HTML elements", async () => {
    const xssPayload = "<img src=x onerror=alert(1)>";
    const maliciousItem = buildConflict({
      reason: `Conflict reason ${xssPayload}`,
      side_a: {
        ...buildConflict().side_a,
        text_snapshot: `Policy text ${xssPayload}`,
      },
      side_b: {
        ...buildConflict().side_b,
        text_snapshot: `Updated text ${xssPayload}`,
      },
    });

    listSpy.mockResolvedValueOnce({
      items: [maliciousItem],
      total: 1,
      limit: 20,
      offset: 0,
    });

    const { container, unmount } = mountInbox();
    await flushPromises();

    expect(container.querySelectorAll("img").length).toBe(0);
    expect(container.textContent).toContain(`Policy text ${xssPayload}`);
    expect(container.textContent).toContain(`Updated text ${xssPayload}`);
    expect(container.textContent).toContain(`Conflict reason ${xssPayload}`);
    unmount();
  });

  it("prevents stale-closure tab overwrite during in-flight resolveConflict and blocks synchronous double-clicks", async () => {
    const pendingItem = buildConflict({
      id: "res-race-pending",
      status: "PENDING_REVIEW",
      side_b: {
        ...buildConflict().side_b,
        document_name: "Pending_Only_Policy.pdf",
      },
    });
    const activeItem = buildConflict({
      id: "res-race-active",
      status: "ACTIVE",
      resolution_mode: "MANUAL_HITL",
      side_b: {
        ...buildConflict().side_b,
        document_name: "Active_Tab_Policy.pdf",
      },
    });

    let resolveMutation!: (val: ConflictActionResponse) => void;
    resolveSpy.mockImplementation(
      () =>
        new Promise<ConflictActionResponse>((resolve) => {
          resolveMutation = resolve;
        }),
    );

    listSpy.mockImplementation(async (params?: ConflictListParams) => {
      const statuses = params?.status ?? [];
      if (statuses.includes("ACTIVE")) {
        return { items: [activeItem], total: 1, limit: 20, offset: 0 };
      }
      return { items: [pendingItem], total: 1, limit: 20, offset: 0 };
    });

    const { container, unmount } = mountInbox();
    await flushPromises();

    // Rapid synchronous double-click on Keep Side B must call resolveConflict exactly ONCE
    const keepB = findButtonByText(container, "Keep Side B")!;
    act(() => {
      keepB.click();
      keepB.click();
    });
    expect(resolveSpy).toHaveBeenCalledTimes(1);

    // Switch tab to Active while resolveConflict is still in-flight
    const activeTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent?.trim() === "Active",
    ) as HTMLElement;
    await act(async () => {
      activeTab.dispatchEvent(
        new MouseEvent("mousedown", { bubbles: true, button: 0 }),
      );
      activeTab.click();
    });
    await flushPromises();
    expect(container.textContent).toContain("Active_Tab_Policy.pdf");
    expect(container.textContent).not.toContain("Pending_Only_Policy.pdf");

    // Now finish the in-flight resolve mutation from the old pending tab
    await act(async () => {
      resolveMutation(buildActionResponse({ id: "res-race-pending" }));
    });
    await flushPromises();

    // Post-mutation reload MUST query the currently active tab ("ACTIVE"), NEVER stale "pending"
    expect(listSpy).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: ["ACTIVE"] }),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(container.textContent).toContain("Active_Tab_Policy.pdf");
    expect(container.textContent).not.toContain("Pending_Only_Policy.pdf");
    unmount();
  });

  it("clears stale cards from previous tab when a new tab fetch fails", async () => {
    const pendingItem = buildConflict({
      id: "res-prev-tab",
      side_b: { ...buildConflict().side_b, document_name: "Old_Pending_Doc.pdf" },
    });
    listSpy
      .mockResolvedValueOnce({
        items: [pendingItem],
        total: 1,
        limit: 20,
        offset: 0,
      })
      .mockRejectedValueOnce(new ApiError(500, "internal", "Active tab failed"));

    const { container, unmount } = mountInbox();
    await flushPromises();
    expect(container.textContent).toContain("Old_Pending_Doc.pdf");

    const activeTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent?.trim() === "Active",
    ) as HTMLElement;
    await act(async () => {
      activeTab.dispatchEvent(
        new MouseEvent("mousedown", { bubbles: true, button: 0 }),
      );
      activeTab.click();
    });
    await flushPromises();

    expect(container.textContent).toContain("Active tab failed");
    expect(container.textContent).not.toContain("Old_Pending_Doc.pdf");
    unmount();
  });

  it("clamps pagination cleanly to offset=0 when offset=40 drops to total=0 without Page 2 / 1 flash", async () => {
    const pageItems = (prefix: string) =>
      Array.from({ length: 20 }, (_, i) =>
        buildConflict({ id: `${prefix}-${i}` }),
      );

    listSpy
      .mockResolvedValueOnce({ items: pageItems("p1"), total: 45, limit: 20, offset: 0 })
      .mockResolvedValueOnce({ items: pageItems("p2"), total: 45, limit: 20, offset: 20 })
      .mockResolvedValueOnce({
        items: [buildConflict({ id: "p3-0" })],
        total: 45,
        limit: 20,
        offset: 40,
      })
      // Refresh at offset=40 sees total=0 -> should clamp directly to offset=0
      .mockResolvedValueOnce({ items: [], total: 0, limit: 20, offset: 40 })
      .mockResolvedValueOnce({ items: [], total: 0, limit: 20, offset: 0 });

    const { container, unmount } = mountInbox();
    await flushPromises();

    // Go to Page 2 (offset=20), then Page 3 (offset=40)
    await act(async () => {
      findButtonByText(container, "Next")!.click();
    });
    await flushPromises();
    await act(async () => {
      findButtonByText(container, "Next")!.click();
    });
    await flushPromises();
    expect(container.textContent).toContain("Page 3 / 3");

    // Trigger refresh -> backend reports empty page with total=0
    await act(async () => {
      findButtonByText(container, "Refresh")!.click();
    });
    await flushPromises();

    expect(listSpy).toHaveBeenLastCalledWith(
      expect.objectContaining({ offset: 0 }),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(container.textContent).not.toContain("Page 2 / 1");
    expect(container.textContent).not.toContain("Page 3 / 1");
    expect(findButtonByText(container, "Previous")).toBeUndefined();
    expect(findButtonByText(container, "Next")).toBeUndefined();
    unmount();
  });

  it("jumps directly to last valid page in 1 step when offset=40 drops to total=15", async () => {
    const pageItems = (prefix: string, count: number) =>
      Array.from({ length: count }, (_, i) =>
        buildConflict({ id: `${prefix}-${i}` }),
      );

    listSpy
      .mockResolvedValueOnce({ items: pageItems("p1", 20), total: 45, limit: 20, offset: 0 })
      .mockResolvedValueOnce({ items: pageItems("p2", 20), total: 45, limit: 20, offset: 20 })
      .mockResolvedValueOnce({ items: pageItems("p3", 5), total: 45, limit: 20, offset: 40 })
      // Refresh at offset=40 sees total=15 -> must jump directly to offset=0 (NOT offset=20!)
      .mockResolvedValueOnce({ items: [], total: 15, limit: 20, offset: 40 })
      .mockResolvedValueOnce({ items: pageItems("clamped", 15), total: 15, limit: 20, offset: 0 });

    const { container, unmount } = mountInbox();
    await flushPromises();

    await act(async () => {
      findButtonByText(container, "Next")!.click();
    });
    await flushPromises();
    await act(async () => {
      findButtonByText(container, "Next")!.click();
    });
    await flushPromises();

    await act(async () => {
      findButtonByText(container, "Refresh")!.click();
    });
    await flushPromises();

    // Exactly 5 calls: offset 0 -> 20 -> 40 -> refresh(40) -> direct clamp(0), zero intermediate offset=20!
    expect(listSpy).toHaveBeenCalledTimes(5);
    const offsetsCalled = listSpy.mock.calls.map(
      (call: [ConflictListParams | undefined, ...unknown[]]) => call[0]?.offset,
    );
    expect(offsetsCalled).toEqual([0, 20, 40, 40, 0]);
    expect(container.textContent).toContain("Page 1 / 1");
    expect(findButtonByText(container, "Next")).toBeUndefined();
    unmount();
  });

  it("renders nullable version_no and cosine_similarity cleanly without v0 or NaN%", async () => {
    const nullableItem = buildConflict({
      cosine_similarity: null,
      side_a: {
        ...buildConflict().side_a,
        version_no: null,
      },
      side_b: {
        ...buildConflict().side_b,
        version_no: null,
      },
    });
    listSpy.mockResolvedValueOnce({
      items: [nullableItem],
      total: 1,
      limit: 20,
      offset: 0,
    });

    const { container, unmount } = mountInbox();
    await flushPromises();

    const text = container.textContent ?? "";
    expect(text).toContain("HR_Leave_Policy_2024.pdf");
    expect(text).toContain("Board_Resolution_Leave_2026.pdf");
    expect(text).toContain("Conf 92%");
    expect(text).not.toContain("Cos ");
    expect(text).not.toContain("NaN%");
    expect(text).not.toMatch(/\bv0\b/);
    expect(text).not.toContain("vnull");
    unmount();
  });
});
