import type {
  UniverseKnowledgeEdge,
  UniverseKnowledgeMatch,
  UniverseKnowledgeUnit,
  UniverseTree,
  UniverseTreeNode,
} from "./types";
import type { UniverseSceneData, UniverseSceneNode } from "@/components/features/universe-scene-contract";

export interface KnowledgeCluster {
  project_id: string;
  tree_version: string;
  node_id: string;
}

export function knowledgeClusterKey(cluster: KnowledgeCluster) {
  return `cluster:${[cluster.project_id, cluster.tree_version, cluster.node_id].map(encodeURIComponent).join(":")}`;
}

export function knowledgeUnitKey(cluster: KnowledgeCluster, unitId: string) {
  return `unit:${encodeURIComponent(cluster.project_id)}:${encodeURIComponent(cluster.tree_version)}:${encodeURIComponent(unitId)}`;
}

export function findKnowledgeCluster(trees: UniverseTree[], cluster: KnowledgeCluster | null) {
  if (!cluster) return null;
  const tree = trees.find((item) => item.project_id === cluster.project_id && item.tree_version === cluster.tree_version);
  const node = tree?.nodes.find((item) => item.id === cluster.node_id);
  return tree && node ? { tree, node } : null;
}

export function knowledgeClusterTrail(trees: UniverseTree[], cluster: KnowledgeCluster | null) {
  const current = findKnowledgeCluster(trees, cluster);
  if (!current) return [];
  const byId = new Map(current.tree.nodes.map((node) => [node.id, node]));
  const trail: UniverseTreeNode[] = [];
  const seen = new Set<string>();
  let node: UniverseTreeNode | undefined = current.node;
  while (node && !seen.has(node.id)) {
    seen.add(node.id);
    trail.unshift(node);
    node = node.parent_id ? byId.get(node.parent_id) : undefined;
  }
  return trail;
}

function seededUnit(value: string) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) hash = Math.imul(hash ^ value.charCodeAt(index), 16777619);
  return (hash >>> 0) / 4294967296;
}

export interface DocumentGanglionGroup {
  document_id: string;
  document_name: string;
  source_id: string;
  source_name: string;
  version_no: number;
  units: UniverseKnowledgeUnit[];
  e2_count: number;
  cross_doc_synapse_count: number;
}

export function groupUnitsByDocument(
  units: UniverseKnowledgeUnit[],
  edges: UniverseKnowledgeEdge[] = [],
): DocumentGanglionGroup[] {
  const byDoc = new Map<string, DocumentGanglionGroup>();
  const docByUnit = new Map<string, string>();
  for (const unit of units) {
    docByUnit.set(unit.id, unit.document_id);
    let group = byDoc.get(unit.document_id);
    if (!group) {
      group = {
        document_id: unit.document_id,
        document_name: unit.document_name,
        source_id: unit.source_id,
        source_name: unit.source_name,
        version_no: unit.version_no,
        units: [],
        e2_count: 0,
        cross_doc_synapse_count: 0,
      };
      byDoc.set(unit.document_id, group);
    }
    group.units.push(unit);
    if (unit.neural_tier === "E2") {
      group.e2_count += 1;
    }
  }
  for (const group of byDoc.values()) {
    const unitCrossSum = group.units.reduce((acc, u) => acc + (u.cross_doc_synapse_count ?? 0), 0);
    if (unitCrossSum > 0) {
      group.cross_doc_synapse_count = unitCrossSum;
    } else {
      let edgeCross = 0;
      for (const edge of edges) {
        const dA = docByUnit.get(edge.source);
        const dB = docByUnit.get(edge.target);
        if (
          (dA === group.document_id && dB && dB !== group.document_id)
          || (dB === group.document_id && dA && dA !== group.document_id)
          || (edge.cross_document && (dA === group.document_id || dB === group.document_id))
        ) {
          edgeCross += 1;
        }
      }
      group.cross_doc_synapse_count = edgeCross;
    }
  }
  return Array.from(byDoc.values());
}

export function projectKnowledgeUniverse(input: {
  clusters: Array<{ ref: KnowledgeCluster; node: UniverseTreeNode; label: string; stats: string }>;
  active: KnowledgeCluster | null;
  units: UniverseKnowledgeUnit[];
  edges: UniverseKnowledgeEdge[];
  matches: UniverseKnowledgeMatch[];
  routedNodes: Set<string>;
  matchedClusters?: Set<string>;
  unitKind: string;
  clusterKind: string;
  relatedHint: string;
  relationsAvailable?: boolean;
  epoch: number;
}): UniverseSceneData {
  const { clusters, active, units, matches } = input;
  const matchedIds = new Set(matches.flatMap((match) =>
    match.project_id === active?.project_id && match.tree_version === active.tree_version ? match.unit_ids : [],
  ));
  // Source/event are renderer archetypes (nebula/star), never backend domain types.
  const nodes: UniverseSceneNode[] = clusters.map(({ ref, node, label, stats }, index) => {
    const key = knowledgeClusterKey(ref);
    const angle = index * 2.399963229728653;
    const spread = clusters.length === 1 ? 0 : 310 * Math.sqrt(index + 0.6);
    return {
      id: key, rawId: node.id, sourceId: key, kind: "source", kindLabel: input.clusterKind,
      label, statsLabel: stats, description: stats, category: input.clusterKind,
      radius: Math.min(170, 100 + Math.log2(node.unit_count + 1) * 9),
      density: Math.min(1, Math.log2(node.unit_count + 1) / 9),
      eventCount: node.unit_count, entityCount: 0, relationCount: 0, relatedCount: node.unit_count,
      relatedCountKnown: true, importance: input.routedNodes.has(key) ? 1 : 0.5,
      statsReady: true, state: input.routedNodes.has(key) ? "active" : "latent", root: true,
      routeMatch: input.routedNodes.has(key), searchMatch: input.matchedClusters?.has(key),
      x: Math.cos(angle) * spread, y: Math.sin(angle) * spread * 0.68,
      z: (seededUnit(key) - 0.5) * (clusters.length === 1 ? 0 : 160),
    };
  });
  if (active) {
    const group = knowledgeClusterKey(active);
    const docGroups = groupUnitsByDocument(units, input.edges);
    const multiDoc = docGroups.length > 1;
    const docIndexById = new Map(docGroups.map((g, idx) => [g.document_id, idx]));
    const docLocalIndex = new Map<string, number>();

    units.forEach((unit, index) => {
      const key = knowledgeUnitKey(active, unit.id);
      let x = 0;
      let y = 0;
      let z = 0;
      if (!multiDoc) {
        const angle = (units.length <= 8 ? index * Math.PI * 2 / units.length : index * 2.399963229728653)
          + seededUnit(group) * Math.PI * 2;
        const radius = units.length <= 8 ? 130 : 62 + Math.sqrt(index + 1) * 31;
        x = Math.cos(angle) * radius;
        y = Math.sin(angle) * radius * 0.8;
        z = (seededUnit(unit.id) - 0.5) * 12;
      } else {
        const dIdx = docIndexById.get(unit.document_id) ?? 0;
        const dGroup = docGroups[dIdx];
        const localIdx = docLocalIndex.get(unit.document_id) ?? 0;
        docLocalIndex.set(unit.document_id, localIdx + 1);
        const docAngle = (dIdx * Math.PI * 2) / docGroups.length + seededUnit(group) * Math.PI * 0.5;
        const docOrbit = 135 + Math.min(65, docGroups.length * 14);
        const centerX = Math.cos(docAngle) * docOrbit;
        const centerY = Math.sin(docAngle) * docOrbit * 0.78;
        const centerZ = (seededUnit(`doc:${unit.document_id}`) - 0.5) * 42;
        const localCount = Math.max(1, dGroup?.units.length ?? 1);
        const localAngle = (localCount <= 6 ? (localIdx * Math.PI * 2) / localCount : localIdx * 2.399963229728653)
          + seededUnit(unit.document_id) * Math.PI * 2;
        const localRadius = localCount === 1 ? 0 : 34 + Math.sqrt(localIdx + 1) * 20;
        x = centerX + Math.cos(localAngle) * localRadius;
        y = centerY + Math.sin(localAngle) * localRadius * 0.82;
        z = centerZ + (seededUnit(unit.id) - 0.5) * 10;
      }
      const isE2 = unit.neural_tier === "E2";
      const tierPrefix = unit.neural_tier
        ? (isE2 ? "E2 Pyramidal · " : "E0/E1 · ")
        : "";
      const crossSuffix = (unit.cross_doc_synapse_count ?? 0) > 0
        ? ` · ⇄${unit.cross_doc_synapse_count}`
        : "";
      nodes.push({
        id: key, rawId: unit.id, sourceId: group, kind: "event", kindLabel: input.unitKind,
        label: unit.label, description: unit.preview,
        category: `${tierPrefix}${unit.document_name}${crossSuffix}`,
        radius: isE2 ? 11 : 8, density: unit.importance ?? 0, eventCount: 0, entityCount: 0,
        relationCount: input.edges.filter((edge) => edge.source === unit.id || edge.target === unit.id).length,
        relatedCount: 0, relatedCountKnown: false, canExploreMore: input.relationsAvailable !== false,
        progressLabel: input.relatedHint,
        importance: matchedIds.has(unit.id) ? 1 : Math.max(0.45, unit.importance ?? 0.45),
        statsReady: true, state: "active", root: matchedIds.has(unit.id),
        searchMatch: matchedIds.has(unit.id),
        presentationScale: isE2 ? 1.22 : undefined,
        x, y, z,
      });
    });
  }
  const visible = new Set(nodes.map((node) => node.id));
  const hierarchyLinks = clusters.flatMap(({ ref, node }) => {
    if (!node.parent_id) return [];
    const parent = knowledgeClusterKey({ ...ref, node_id: node.parent_id });
    const child = knowledgeClusterKey(ref);
    return visible.has(parent)
      ? [{ id: `${parent}>${child}`, source: parent, target: child, weight: 0.25, virtual: true }]
      : [];
  });
  return {
    epoch: input.epoch, nodes,
    links: [...hierarchyLinks, ...(active ? input.edges.flatMap((edge) => {
      const source = knowledgeUnitKey(active, edge.source);
      const target = knowledgeUnitKey(active, edge.target);
      return visible.has(source) && visible.has(target)
        ? [{
          id: `${source}~${target}`,
          source,
          target,
          weight: edge.cross_document ? Math.max(edge.weight, 0.78) : edge.weight,
          virtual: false,
          presentationOpacity: edge.cross_document ? 1.45 : undefined,
        }]
        : [];
    }) : [])],
  };
}
