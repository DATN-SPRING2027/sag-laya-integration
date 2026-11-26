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
  ConflictListResponse,
} from "@/lib/types";
import { ConflictInbox } from "@/components/features/conflict-inbox";

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
    listSpy = vi.spyOn(api, "listConflicts");
    resolveSpy = vi.spyOn(api, "resolveConflict");
    reopenSpy = vi.spyOn(api, "reopenConflict");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    document.body.innerHTML = "";
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

  it("passes source_id from initialSourceId and removes it when source pill clear button is clicked", async () => {
    listSpy.mockResolvedValue({
      items: [buildConflict()],
      total: 1,
      limit: 20,
      offset: 0,
    });

    const { container, unmount } = mountInbox({ initialSourceId: "src-hr-99" });
    await flushPromises();

    expect(listSpy).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({ source_id: "src-hr-99" }),
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
    expect(listSpy).toHaveBeenLastCalledWith(
      expect.objectContaining({ source_id: undefined }),
    );
    unmount();
  });

  it("expands the collapsible Audit Trail and displays history entries", async () => {
    listSpy.mockResolvedValueOnce({
      items: [buildConflict()],
      total: 1,
      limit: 20,
      offset: 0,
    });

    const { container, unmount } = mountInbox();
    await flushPromises();

    expect(container.textContent).not.toContain("FLAGGED_FOR_HITL");

    const auditToggle = findButtonByText(container, "Audit trail (2)")!;
    expect(auditToggle).toBeDefined();
    expect(auditToggle.getAttribute("aria-expanded")).toBe("false");

    act(() => {
      auditToggle.click();
    });

    expect(auditToggle.getAttribute("aria-expanded")).toBe("true");
    expect(container.textContent).toContain("DETECTED");
    expect(container.textContent).toContain("FLAGGED_FOR_HITL");
    expect(container.textContent).toContain("Actor: policy-engine");
    expect(container.textContent).toContain(
      "Requires human sign-off before enforcing precedence.",
    );
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
});
