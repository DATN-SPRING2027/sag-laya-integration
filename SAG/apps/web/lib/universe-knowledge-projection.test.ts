import { describe, expect, it } from "vitest";
import { activationFromCitations, activationFromSearch } from "./universe-events";
import {
  groupUnitsByDocument,
  knowledgeClusterKey,
  knowledgeClusterTrail,
  projectKnowledgeUniverse,
} from "./universe-knowledge-projection";
import type { SearchResponse, UniverseKnowledgeUnit, UniverseTree } from "./types";

const cluster = { project_id: "project", tree_version: "tree-version", node_id: "leaf" };
const node = { id: "leaf", parent_id: null, depth: 0, label: "Retrieval", is_leaf: true, unit_count: 5, source_count: 1 };
const units: UniverseKnowledgeUnit[] = Array.from({ length: 5 }, (_, index) => ({
  id: `unit-${index}`, label: `Passage ${index}`, preview: `Canonical evidence ${index}`,
  source_id: "source", source_name: "Notes", document_id: "document", document_version_id: "version",
  document_name: "retrieval.md", version_no: 1, block_id: `block-${index}`, heading: "Retrieval",
  page_from: 1, page_to: 1, anchor: `anchor-${index}`,
}));

function projection() {
  return projectKnowledgeUniverse({
    clusters: [{ ref: cluster, node, label: node.label, stats: "5 knowledge units · 1 source" }],
    active: cluster, units, edges: [
      { source: "unit-0", target: "unit-1", weight: 0.7, kind: "similarity" },
      { source: "unit-0", target: "hidden-unit", weight: 0.9, kind: "similarity" },
    ],
    matches: [], routedNodes: new Set(), unitKind: "Knowledge unit", clusterKind: "Knowledge cluster",
    relatedHint: "Explore related knowledge", epoch: 1,
  });
}

describe("knowledge universe projection", () => {
  it("opens a one-leaf corpus as its five real knowledge units without inventing entities", () => {
    const data = projection();
    expect(data.nodes).toHaveLength(6);
    expect(data.nodes.filter((item) => item.kind === "source")).toHaveLength(1);
    expect(data.nodes.filter((item) => item.kind === "entity")).toHaveLength(0);
    expect(data.nodes.filter((item) => item.kindLabel === "Knowledge unit").map((item) => item.rawId))
      .toEqual(units.map((unit) => unit.id));
    expect(data.nodes[0].statsLabel).toBe("5 knowledge units · 1 source");
    expect(data.temporalFlight).toBeUndefined();
  });

  it("draws only links whose two endpoints are actually visible", () => {
    const data = projection();
    expect(data.links).toHaveLength(1);
    const ids = new Set(data.nodes.map((item) => item.id));
    expect(data.links.every((link) => ids.has(link.source) && ids.has(link.target))).toBe(true);
  });

  it("presents source groups without inventing routing or related-knowledge links", () => {
    const source = { ...cluster, tree_version: "source-snapshot", node_id: "source:source" };
    const data = projectKnowledgeUniverse({
      clusters: [{ ref: source, node: { ...node, id: source.node_id }, label: "Notes", stats: "5 units" }],
      active: source, units, edges: [], matches: [], routedNodes: new Set(),
      unitKind: "Knowledge unit", clusterKind: "Knowledge source", relatedHint: "Waiting for tree sync",
      relationsAvailable: false, epoch: 1,
    });
    expect(data.nodes[0].kindLabel).toBe("Knowledge source");
    expect(data.links).toEqual([]);
    expect(data.nodes.filter((item) => item.kind === "event").every((item) => item.canExploreMore === false)).toBe(true);
  });

  it("keeps deterministic positions and separates identities across projects and snapshots", () => {
    expect(projection().nodes.map(({ id, x, y, z }) => ({ id, x, y, z })))
      .toEqual(projection().nodes.map(({ id, x, y, z }) => ({ id, x, y, z })));
    expect(knowledgeClusterKey(cluster)).not.toBe(knowledgeClusterKey({ ...cluster, project_id: "other" }));
    expect(knowledgeClusterKey(cluster)).not.toBe(knowledgeClusterKey({ ...cluster, tree_version: "new" }));
  });

  it("preserves the actual hierarchy and bounds traversal of malformed cycles", () => {
    const tree: UniverseTree = {
      ...cluster, search_epoch: 2, node_count: 2, unit_count: 5, source_count: 1,
      nodes: [{ ...node, id: "root", is_leaf: false }, { ...node, parent_id: "root", depth: 1 }],
    };
    expect(knowledgeClusterTrail([tree], cluster).map((item) => item.id)).toEqual(["root", "leaf"]);
    tree.nodes[0].parent_id = "leaf";
    expect(knowledgeClusterTrail([tree], cluster)).toHaveLength(2);
  });

  it("clusters multi-document neurons into distinct Document Ganglia and highlights E2 & cross-document axons", () => {
    const multiDocUnits: UniverseKnowledgeUnit[] = [
      {
        ...units[0],
        id: "u-doc1",
        document_id: "doc-1",
        document_name: "policy.md",
        neural_tier: "E2",
        importance: 0.85,
        cross_doc_synapse_count: 1,
      },
      {
        ...units[1],
        id: "u-doc2",
        document_id: "doc-2",
        document_name: "procedure.md",
        neural_tier: "E0_E1",
        importance: 0.45,
        cross_doc_synapse_count: 1,
      },
    ];
    const groups = groupUnitsByDocument(multiDocUnits, [
      { source: "u-doc1", target: "u-doc2", weight: 0.65, kind: "similarity", cross_document: true },
    ]);
    expect(groups).toHaveLength(2);
    expect(groups[0].e2_count).toBe(1);
    expect(groups[0].cross_doc_synapse_count).toBe(1);

    const data = projectKnowledgeUniverse({
      clusters: [{ ref: cluster, node, label: node.label, stats: "2 neurons · 2 docs" }],
      active: cluster,
      units: multiDocUnits,
      edges: [{ source: "u-doc1", target: "u-doc2", weight: 0.65, kind: "similarity", cross_document: true }],
      matches: [],
      routedNodes: new Set(),
      unitKind: "Knowledge unit",
      clusterKind: "Knowledge cluster",
      relatedHint: "Explore related knowledge",
      epoch: 1,
    });
    const n1 = data.nodes.find((n) => n.rawId === "u-doc1")!;
    const n2 = data.nodes.find((n) => n.rawId === "u-doc2")!;
    expect(n1.radius).toBeGreaterThan(n2.radius);
    expect(n1.category).toContain("E2 Pyramidal");
    expect(n1.category).toContain("⇄1");
    expect(data.links[0].presentationOpacity).toBeGreaterThan(1);
  });
});

describe("canonical evidence activation", () => {
  it("preserves SearchUnit and block references when the search returns no legacy events", () => {
    const response: SearchResponse = {
      query: "How does routing work?", events: [], entities: [], relations: [], source_hits: [],
      sections: [{ chunk_id: "block", search_unit_id: "search-unit", document_version_id: "version",
        source_id: "source", heading: "Routing", content: "Evidence", score: 0.5, rank: 1 }],
      summary: "", exploration_id: null, stats: {},
    };
    const activation = activationFromSearch(response);
    expect(activation.nodes).toEqual([]);
    expect(activation.evidence_refs).toEqual([{
      source_id: "source", block_id: "block", search_unit_id: "search-unit", document_version_id: "version",
    }]);
  });

  it("uses internal answer citations and preserves the routing trace", () => {
    const previous = { query: "", nodes: [], relations: [], tree_routes: [{ ...cluster, node_ids: ["leaf"] }] };
    const activation = activationFromCitations("Explain routing", [
      { n: 1, kind: "internal", source_id: "source", chunk_id: "block", heading: "Routing", snippet: "Evidence", score: 1 },
      { n: 2, kind: "external", source_id: null, chunk_id: null, heading: "Web", snippet: "", score: 1 },
    ], previous);
    expect(activation.evidence_refs).toEqual([{ source_id: "source", block_id: "block" }]);
    expect(activation.tree_routes).toEqual(previous.tree_routes);
    expect(activation.origin).toBe("assistant");
  });
});
