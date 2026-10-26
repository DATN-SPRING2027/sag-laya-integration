import { describe, expect, it } from "vitest";
import { knowledgeDocumentTitle, knowledgePassage, knowledgeUnitTitle, knowledgePreviewScore, knowledgeRegionTitle } from "./knowledge-presentation";
import type { UniverseKnowledgeUnit } from "./types";

describe("knowledge presentation", () => {
  it("preserves the source offset after YAML metadata for anchored evidence", () => {
    const content = '---\r\ndocument_id: "DOC-1"\r\ntitle: "Payments"\r\n---\r\n\r\nUse the event identifier.';
    const passage = knowledgePassage(content);
    expect(passage.text).toBe("Use the event identifier.");
    expect(content.slice(passage.offset)).toBe(passage.text);
    const evidenceStart = content.indexOf("event identifier");
    expect(passage.text.slice(evidenceStart - passage.offset, evidenceStart - passage.offset + 16)).toBe("event identifier");
  });

  it("keeps ordinary source paragraphs intact", () => {
    expect(knowledgePassage("  Valid evidence.\nSecond line.")).toEqual({ text: "Valid evidence.\nSecond line.", offset: 2 });
  });

  it("uses the document title when the available preview contains only metadata", () => {
    expect(knowledgeUnitTitle({ label: 'document_id: "DOC-1" title: "Payments"', preview: '---\ndocument_id: "DOC-1"', heading: "", document_name: "Payment_Orchestration_ADR_v2.md" })).toBe("Payment Orchestration ADR");
  });

  it("retains meaningful passage labels", () => {
    expect(knowledgeUnitTitle({ label: "**Duplicate webhook handling**", preview: "", heading: "", document_name: "payments.md" })).toBe("Duplicate webhook handling");
    expect(knowledgeDocumentTitle("Pilot_Weekly_Decisions_2026-10-09.md")).toBe("Pilot Weekly Decisions 2026-10-09");
  });

  it("uses the actual section heading instead of a truncated sentence", () => {
    expect(knowledgeUnitTitle({label:"A long paragraph ".repeat(8),preview:"More text ".repeat(20),heading:"Payment architecture > Webhook Deduplication Protocol",document_name:"Payments.md"})).toBe("Webhook Deduplication Protocol");
    const unit = { preview: "Clear actionable guidance with evidence.", heading: "Document > Protocol", cross_doc_synapse_count: 3 } as UniverseKnowledgeUnit;
    expect(knowledgePreviewScore(unit)).toBeGreaterThan(knowledgePreviewScore({ ...unit, preview: "--- document_id: DOC-1" }));
    expect(knowledgePreviewScore(unit)).toBeGreaterThan(knowledgePreviewScore({ ...unit, document_name: "Payment_v1_SUPERSEDED.md" }));
  });

  it("derives a region title from its readable contents without treating routing codes as topics", () => {
    const units = ["Customer_Order_Fulfillment.docx", "Refund_Policy.md"].map((document_name) => ({ document_name, preview: "Actual source content" }) as UniverseKnowledgeUnit);
    expect(knowledgeRegionTitle(units, "AUTHORIZED")).toBe("Orders & fulfillment");
    expect(knowledgeRegionTitle(units.map((unit) => ({ ...unit, preview: "--- document_id: metadata" })), "AUTHORIZED")).toBeNull();
  });
});
