import { describe, expect, it } from "vitest";
import * as THREE from "three";
import type { NeuralCortexNeuronItem, NeuralCortexSceneProps } from "./neural-atlas-contract";
import { ATLAS_FRAMING, atlasConnectionCurve, buildAtlasLayout, projectAtlasLabel } from "./neural-atlas-layout";

const neurons = Array.from({ length: 40 }, (_, index) => ({
  key: `unit-${index}`, clusterKey: `region-${Math.floor(index / 8)}`, cleanLabel: `Passage ${index}`,
  isSelected: false, isMatched: false,
  unit: { id: `unit-${index}`, label: `Passage ${index}`, heading: "Document > Protocol", preview: "A meaningful passage with supporting evidence and related source material.", document_name: "Document.md", evidence_count: index % 5, cross_doc_synapse_count: index % 8 },
})) as NeuralCortexNeuronItem[];
const input = { neurons, regions: [], edges: [], filterMode: "all" } satisfies Pick<NeuralCortexSceneProps, "neurons" | "regions" | "edges" | "filterMode">;

describe("atlas composition", () => {
  it("retains real knowledge identities and bounds markers by their evidence count", () => {
    const layout = buildAtlasLayout(input);
    expect([...layout.points.keys()].sort()).toEqual(neurons.map((neuron) => neuron.key).sort());
    for (const neuron of neurons) {
      const beacons = layout.beacons.filter((beacon) => beacon.parentKey === neuron.key);
      expect(beacons.length).toBeLessThanOrEqual(neuron.unit.evidence_count!);
      expect(beacons.length).toBeLessThanOrEqual(10);
    }
  });

  it("keeps node positions fixed when input order or selected relations change", () => {
    const baseline = buildAtlasLayout(input);
    const updated = buildAtlasLayout({ ...input, neurons: [...neurons].reverse(), edges: [{ id: "extra", sourceKey: "unit-0", targetKey: "unit-9", weight: 1, kind: "cross_doc_axon" }] });
    for (const [key, point] of baseline.points) expect(updated.points.get(key)?.position.toArray()).toEqual(point.position.toArray());
  });

  it("places the foreground focal node at the reference composition", () => {
    const layout = buildAtlasLayout(input);
    const hero = [...layout.points.values()].find((point) => point.group === 0 && point.radius === 9)!;
    const camera = new THREE.PerspectiveCamera(ATLAS_FRAMING.fov, ATLAS_FRAMING.aspect, 1, 3500);
    camera.position.set(0, 0, ATLAS_FRAMING.distance); camera.updateMatrixWorld();
    const point = hero.position.clone().project(camera);
    expect((point.x * .5 + .5) * 1114 + 80).toBeCloseTo(421.0, -1);
    expect((-point.y * .5 + .5) * 838 + 62).toBeCloseTo(511.0, -1);
  });

  it("filters hidden points and their links together", () => {
    const layout = buildAtlasLayout({ ...input, filterMode: "e2_only", edges: [{ id: "hidden", sourceKey: "unit-0", targetKey: "unit-9", weight: 1, kind: "cross_doc_axon" }] });
    expect(layout.points.size).toBe(0);
    expect(layout.edges).toEqual([]);
    expect(layout.beacons).toEqual([]);
  });

  it("anchors each region label to its cluster center through camera rotation, pan and zoom", () => {
    const regions = Array.from({ length: 5 }, (_, index) => ({ key: `region-${index}`, label: `Region ${index}` })) as NeuralCortexSceneProps["regions"];
    const layout = buildAtlasLayout({ ...input, regions });
    const camera = new THREE.PerspectiveCamera(35, 1.5, 1, 3500);
    for (const eye of [[0, 0, 1000], [800, 400, 500], [-600, -150, 900]]) {
      camera.position.set(...eye as [number, number, number]);
      camera.lookAt(50, -30, 0); camera.updateMatrixWorld();
      for (const label of layout.labels) {
        const anchor = projectAtlasLabel(label.position, camera, 1200, 800);
        const center = layout.centers.get(label.region.key)!.clone().project(camera);
        expect(anchor.x).toBeCloseTo((center.x * .5 + .5) * 1200);
        expect(anchor.y).toBeCloseTo((-center.y * .5 + .5) * 800);
      }
    }
  });

  it("hides anchors outside the viewport or behind the camera", () => {
    const camera = new THREE.PerspectiveCamera(35, 1.5, 1, 3500);
    camera.position.set(0, 0, 1000); camera.updateMatrixWorld();
    expect(projectAtlasLabel(new THREE.Vector3(), camera, 1200, 800).visible).toBe(true);
    expect(projectAtlasLabel(new THREE.Vector3(2000, 0, 0), camera, 1200, 800).visible).toBe(false);
    expect(projectAtlasLabel(new THREE.Vector3(0, 0, 1500), camera, 1200, 800).visible).toBe(false);
  });

  it("curves genuine endpoints with different paths rather than parallel offsets", () => {
    const start = new THREE.Vector3(-100, 0, 100), end = new THREE.Vector3(200, 100, -30);
    const first = atlasConnectionCurve(start, end, "first", true), second = atlasConnectionCurve(start, end, "second", true);
    expect(first.getPoint(0).toArray()).toEqual(start.toArray());
    expect(first.getPoint(1).toArray()).toEqual(end.toArray());
    expect(first.getPoint(.5).distanceTo(second.getPoint(.5))).toBeGreaterThan(10);
  });
});
