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
    units.forEach((unit, index) => {
      const key = knowledgeUnitKey(active, unit.id);
      const angle = (units.length <= 8 ? index * Math.PI * 2 / units.length : index * 2.399963229728653)
        + seededUnit(group) * Math.PI * 2;
      const radius = units.length <= 8 ? 130 : 62 + Math.sqrt(index + 1) * 31;
      nodes.push({
        id: key, rawId: unit.id, sourceId: group, kind: "event", kindLabel: input.unitKind,
        label: unit.label, description: unit.preview, category: unit.document_name,
        radius: 8, density: 0, eventCount: 0, entityCount: 0,
        relationCount: input.edges.filter((edge) => edge.source === unit.id || edge.target === unit.id).length,
        relatedCount: 0, relatedCountKnown: false, canExploreMore: input.relationsAvailable !== false,
        progressLabel: input.relatedHint, importance: matchedIds.has(unit.id) ? 1 : 0.45,
        statsReady: true, state: "active", root: matchedIds.has(unit.id),
        searchMatch: matchedIds.has(unit.id),
        x: Math.cos(angle) * radius, y: Math.sin(angle) * radius * 0.8,
        z: (seededUnit(unit.id) - 0.5) * 12,
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
        ? [{ id: `${source}~${target}`, source, target, weight: edge.weight, virtual: false }]
        : [];
    }) : [])],
  };
}
