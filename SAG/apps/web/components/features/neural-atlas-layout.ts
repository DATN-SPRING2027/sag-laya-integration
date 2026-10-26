import * as THREE from "three";
import { knowledgePreviewScore } from "@/lib/knowledge-presentation";
import type { NeuralCortexSceneProps, NeuralCortexNeuronItem, NeuralCortexRegionItem } from "./neural-atlas-contract";

export type AtlasPoint = { neuron: NeuralCortexNeuronItem; position: THREE.Vector3; radius: number; group: number };
export type AtlasBeacon = { parentKey: string; position: THREE.Vector3; radius: number; group: number };
export type AtlasLabel = { region: NeuralCortexRegionItem; position: THREE.Vector3; group: number };

export const ATLAS_FRAMING = { distance: 1000, fov: 35, aspect: 1114 / 838 };
const REGIONS = [
  { center: [-204, -18, 160], reach: [130, 120, 105], label: [-301, 93, 190] },
  { center: [27, 157, -90], reach: [105, 98, 115], label: [83, 240, -90] },
  { center: [370, 174, -300], reach: [92, 84, 140], label: [336, 258, -260] },
  { center: [326, -77, -90], reach: [115, 100, 95], label: [345, -211, -60] },
  { center: [31, -175, 30], reach: [108, 86, 90], label: [75, -265, 50] },
];
const FOREGROUND_HUBS = [[74, 0, 40], [-70, 55, 45], [-28, 40, 50], [0, -55, 65], [75, 52, -10], [-5, 2, 15], [40, -40, 40]];

export function atlasSeed(value: string): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) hash = Math.imul(hash ^ value.charCodeAt(index), 16777619);
  hash = Math.imul(hash ^ hash >>> 16, 0x7feb352d);
  hash = Math.imul(hash ^ hash >>> 15, 0x846ca68b);
  hash ^= hash >>> 16;
  return (hash >>> 0) / 4294967295;
}

export function buildAtlasLayout({ neurons, regions, edges, filterMode }: Pick<NeuralCortexSceneProps, "neurons" | "regions" | "edges" | "filterMode">) {
  const grouped = new Map<string, NeuralCortexNeuronItem[]>();
  for (const neuron of neurons) {
    if (filterMode === "e2_only" && neuron.unit.neural_tier !== "E2") continue;
    if (filterMode === "cross_doc" && !neuron.unit.cross_doc_synapse_count) continue;
    const group = grouped.get(neuron.clusterKey) ?? [];
    group.push(neuron); grouped.set(neuron.clusterKey, group);
  }
  const regionByKey = new Map(regions.map((region) => [region.key, region]));
  const topicOrder = ["Payments", "Partner integrations", "Data platform", "Access & governance", "Search & discovery", "Orders & fulfillment"];
  const rank = (key: string) => { const index = topicOrder.indexOf(regionByKey.get(key)?.label ?? ""); return index < 0 ? 5 : index; };
  const groups = [...grouped].sort((a, b) => rank(a[0]) - rank(b[0]) || b[1].length - a[1].length || a[0].localeCompare(b[0]));
  const points = new Map<string, AtlasPoint>(), centers = new Map<string, THREE.Vector3>();
  const labels: AtlasLabel[] = [], beacons: AtlasBeacon[] = [];
  groups.forEach(([key, items], group) => {
    const shape = REGIONS[group] ?? { center: [Math.cos(group * 2.4) * 290, Math.sin(group * 2.4) * 210, -180], reach: [82, 75, 45], label: [Math.cos(group * 2.4) * 290, Math.sin(group * 2.4) * 210 + 92, -160] };
    const center = new THREE.Vector3(...shape.center);
    if (groups.length === 1) center.set(0, 0, 0);
    centers.set(key, center);
    const hero = [...items].sort((a, b) => knowledgePreviewScore(b.unit) - knowledgePreviewScore(a.unit) || a.key.localeCompare(b.key))[0];
    const ranked = [...items].sort((a, b) => Number(b === hero) - Number(a === hero) || (b.unit.cross_doc_synapse_count ?? 0) - (a.unit.cross_doc_synapse_count ?? 0) || a.key.localeCompare(b.key));
    const evidenceWeight = items.reduce((sum, item) => sum + Math.sqrt(Math.max(0, item.unit.evidence_count ?? 0)), 0);
    const evidenceScale = Math.max(55, items.length * 2.8) / Math.max(1, evidenceWeight);
    ranked.forEach((neuron, ordinal) => {
      const angle = ordinal * Math.PI * (3 - Math.sqrt(5)) + atlasSeed(key) * 2;
      const radial = Math.sqrt((ordinal + .5) / items.length);
      const ripple = 1 + .16 * Math.sin(angle * 3 + .8) + .1 * Math.cos(angle * 5);
      const position = group === 0 && ordinal < FOREGROUND_HUBS.length
        ? new THREE.Vector3(...FOREGROUND_HUBS[ordinal])
        : new THREE.Vector3(Math.cos(angle) * radial * shape.reach[0] * ripple, Math.sin(angle) * radial * shape.reach[1] * (1 + .14 * Math.cos(angle * 2)), (atlasSeed(neuron.key + ":depth") - .5) * shape.reach[2] * 2);
      position.add(center);
      const radius = ordinal === 0 ? 9 : group === 0 && ordinal === 3 ? 9.5 : ordinal < 7 ? 5.5 + atlasSeed(neuron.key) * 2.4 : 1.9 + atlasSeed(neuron.key) ** 2 * 3;
      points.set(neuron.key, { neuron, position, radius, group });
      // Evidence markers show a bounded subset of the real parent evidence count.
      const evidenceCount = Math.max(0, neuron.unit.evidence_count ?? 0);
      const count = Math.min(evidenceCount, 10, Math.round(Math.sqrt(evidenceCount) * evidenceScale));
      for (let index = 0; index < count; index++) {
        const id = `${neuron.key}:evidence:${index}`;
        const direction = atlasSeed(id) * Math.PI * 2;
        const distance = Math.max(15, radius * 2) + atlasSeed(id + ":distance") * 33;
        beacons.push({ parentKey: neuron.key, group, radius: .8 + atlasSeed(id + ":size") ** 2 * 1.6,
          position: position.clone().add(new THREE.Vector3(Math.cos(direction) * distance, Math.sin(direction) * distance, (atlasSeed(id + ":depth") - .5) * 45)) });
      }
    });
    const region = regionByKey.get(key);
    if (region) labels.push({ region, group, position: center.clone() });
  });
  const visibleEdges = edges.filter((edge) => points.has(edge.sourceKey) && points.has(edge.targetKey) && (filterMode !== "cross_doc" || edge.kind === "cross_doc_axon"));
  return { points, beacons, centers, labels, edges: visibleEdges, key: groups.map(([key]) => key).join("|") };
}

export function projectAtlasLabel(position: THREE.Vector3, camera: THREE.PerspectiveCamera, width: number, height: number) {
  const depth = position.clone().applyMatrix4(camera.matrixWorldInverse).z;
  const projected = position.clone().project(camera);
  return {
    x: (projected.x * .5 + .5) * width,
    y: (-projected.y * .5 + .5) * height,
    visible: depth < 0 && Math.abs(projected.x) <= 1 && Math.abs(projected.y) <= 1 && Math.abs(projected.z) <= 1,
  };
}

export function atlasConnectionCurve(start: THREE.Vector3, end: THREE.Vector3, id: string, cross: boolean, sourceGroup?: number, targetGroup?: number) {
  const direction = end.clone().sub(start), distance = direction.length();
  const perpendicular = new THREE.Vector3(-direction.y, direction.x, distance * .18).normalize();
  const bend = (atlasSeed(id) - .5) * distance * (cross ? .55 : .95);
  const a = start.clone().lerp(end, .32).addScaledVector(perpendicular, bend);
  const b = start.clone().lerp(end, .72).addScaledVector(perpendicular, bend * .65);
  if (cross && sourceGroup !== undefined && targetGroup !== undefined && sourceGroup < 5 && targetGroup < 5) {
    const portals: Record<string, number[][]> = {
      "0:1": [[-120, 160, 100], [-20, 210, -40]], "0:2": [[-10, 125, 100], [225, 150, -160]],
      "0:3": [[-10, -130, 100], [170, -100, -10]], "0:4": [[-95, -150, 120], [-10, -160, 40]],
      "1:2": [[140, 190, -110], [250, 165, -220]], "1:3": [[145, 95, -70], [230, -55, -30]],
      "1:4": [[115, 55, -40], [120, -85, 20]], "2:3": [[375, 90, -200], [335, 0, -110]],
      "2:4": [[255, 75, -160], [165, -115, -20]], "3:4": [[220, -170, -30], [90, -150, 35]],
    };
    const route = portals[[sourceGroup, targetGroup].sort((a, b) => a - b).join(":")];
    if (route) {
      const ordered = sourceGroup < targetGroup ? route : [...route].reverse();
      a.lerp(new THREE.Vector3(...ordered[0]), .4); b.lerp(new THREE.Vector3(...ordered[1]), .4);
    }
  }
  a.z += (atlasSeed(id + ":arc") - .5) * (cross ? 95 : 28);
  b.z += (atlasSeed(id + ":arc") - .5) * (cross ? 95 : 28);
  return new THREE.CubicBezierCurve3(start, a, b, end);
}
