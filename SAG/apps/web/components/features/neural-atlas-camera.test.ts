import { describe, expect, it } from "vitest";
import * as THREE from "three";
import { atlasNodeView } from "./neural-atlas-camera";

describe("atlas node camera", () => {
  for (const viewport of [
    { width: 1440, height: 495, right: 1054, bottom: 495 },
    { width: 390, height: 592, right: 390, bottom: 250 },
  ]) {
    it(`zooms onto an already visible node in the available ${viewport.width}px viewport`, () => {
      const camera = new THREE.PerspectiveCamera(35, viewport.width / viewport.height, 1, 3500);
      camera.position.set(700, 300, 1400); camera.lookAt(0, 0, 0); camera.updateMatrixWorld();
      const point = new THREE.Vector3(50, 15, 80), quaternion = camera.quaternion.clone();
      const view = atlasNodeView(camera, point, 95, viewport);
      expect(view.position.distanceTo(point)).toBeLessThan(camera.position.distanceTo(point));
      camera.position.copy(view.position); camera.lookAt(view.target); camera.updateMatrixWorld();
      const screen = point.clone().project(camera);
      expect((screen.x * .5 + .5) * viewport.width).toBeCloseTo(viewport.right / 2);
      expect((-screen.y * .5 + .5) * viewport.height).toBeCloseTo(viewport.bottom / 2);
      expect(camera.quaternion.angleTo(quaternion)).toBeLessThan(.000001);
      const repeated = atlasNodeView(camera, point, 95, viewport);
      expect(repeated.position.distanceTo(view.position)).toBeLessThan(.000001);
    });
  }

  it("keeps extreme viewport sizes finite and inside orbit distance limits", () => {
    const camera = new THREE.PerspectiveCamera(35, .1, 1, 3500);
    const view = atlasNodeView(camera, new THREE.Vector3(0, 0, 100), 95, { width: 100, height: 1000, right: 0, bottom: 0 });
    expect(view.position.toArray().every(Number.isFinite)).toBe(true);
    expect(view.position.distanceTo(view.target)).toBeCloseTo(2600);
  });
});
