import type { UniverseKnowledgeUnit } from "./types";

export function knowledgeDocumentTitle(filename: string): string {
  return filename.replace(/\.(?:docx?|pdf|md|txt)$/i, "")
    .replace(/_v\d+(?:_SUPERSEDED)?$/i, "").replace(/_/g, " ").trim();
}

export function knowledgePassage(content: string): { text: string; offset: number } {
  const frontMatter = content.match(/^\s*---\s*\r?\n[\s\S]*?\r?\n---(?:\s*\r?\n|$)/);
  const offset = frontMatter?.[0].length ?? 0;
  const leadingSpace = content.slice(offset).match(/^\s*/)?.[0].length ?? 0;
  return { text: content.slice(offset + leadingSpace).trimEnd(), offset: offset + leadingSpace };
}

export function knowledgeText(raw: string): string {
  return raw.replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^[\s#>*-]+/gm, "").replace(/[*`_]/g, "")
    .replace(/\|/g, " · ").replace(/\s+/g, " ").trim();
}

const metadata = /^(?:---|(?:document[_ ]id|project[_ ]id|organization[_ ]id|owner[_ ]id|file[_ ]path|access[_ ]groups|created[_ ]at|updated[_ ]at|version|classification|scope|status)\s*[:·|])/i;

export function knowledgeUnitTitle(unit: Pick<UniverseKnowledgeUnit, "label" | "preview" | "heading" | "document_name">): string {
  const section = unit.heading?.includes(">") ? unit.heading.split(">").at(-1)?.trim() : "";
  for (const value of [unit.label, section, knowledgePassage(unit.preview ?? "").text, unit.heading]) {
    const clean = knowledgeText(value ?? "");
    if (clean.length >= 8 && !metadata.test((value ?? "").trim()) && !metadata.test(clean) && !/^\w+[ _]?id\b/i.test(clean)) {
      const title = clean.replace(/\s*\[(?:DOC|PRJ|NSC)[^\]]*\]/g, "");
      if (title.length <= 78) return title;
    }
  }
  return knowledgeDocumentTitle(unit.document_name);
}

export function knowledgePreviewScore(unit: UniverseKnowledgeUnit): number {
  const preview = unit.preview.trim();
  if (/^(?:---|\||document_id|project_id)/i.test(preview) || /superseded|historical|historic|version lineage/i.test(`${unit.document_name} ${unit.heading}`)) return -100;
  const prose = !/^[*#>-]/.test(preview);
  return (prose ? 20 : 0) + (unit.heading.includes(">") ? 8 : 0)
    + Math.min(unit.cross_doc_synapse_count ?? 0, 20) * .4
    - Math.abs(preview.length - 160) / 40;
}

export function knowledgeRegionTitle(units: UniverseKnowledgeUnit[], routingLabel: string): string | null {
  const descriptive = units.filter((unit) => !/^(?:---|\|[\s\S]*Document ID|document_id)/i.test(unit.preview.replace(/\*/g, "").trim()));
  if (!descriptive.length) return null;
  const topics = [
    { label: "Payments", pattern: /payment|checkout/i, hint: /^HTTP$/i },
    { label: "Partner integrations", pattern: /partner|merchant|pulsepay|integration|webhook/i, hint: /meridian/i },
    { label: "Access & governance", pattern: /security|access|audit|governance/i, hint: /authorized|hmac/i },
    { label: "Search & discovery", pattern: /search|catalog|inventory|promotion|analytics/i, hint: /^SKU$/i },
    { label: "Data platform", pattern: /platform|charter|pilot|release|weekly|decision/i, hint: /^DOC$/i },
    { label: "Orders & fulfillment", pattern: /order|fulfillment|refund|cancellation/i, hint: /fulfillment/i },
  ];
  const ranked = topics.map((topic) => { const count = descriptive.filter((unit) => topic.pattern.test(unit.document_name)).length; return { ...topic, score: count + (count && topic.hint.test(routingLabel) ? 2 : 0) }; })
    .sort((a, b) => b.score - a.score);
  return ranked[0].score >= Math.max(2, descriptive.length * .3) ? ranked[0].label : null;
}
