import * as THREE from "three";

export function atlasNodeView(
  camera: THREE.PerspectiveCamera,
  point: THREE.Vector3,
  radius: number,
  viewport: { width: number; height: number; right: number; bottom: number },
) {
  const tangent = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
  const width = Math.max(1, viewport.right), height = Math.max(1, viewport.bottom);
  const distance = THREE.MathUtils.clamp(radius / tangent * Math.max(viewport.height / height, viewport.width / width / camera.aspect), 180, 2600);
  const x = width / viewport.width - 1, y = 1 - height / viewport.height;
  const right = new THREE.Vector3(1, 0, 0).applyQuaternion(camera.quaternion);
  const up = new THREE.Vector3(0, 1, 0).applyQuaternion(camera.quaternion);
  const back = new THREE.Vector3(0, 0, 1).applyQuaternion(camera.quaternion);
  const target = point.clone().addScaledVector(right, -x * distance * tangent * camera.aspect).addScaledVector(up, -y * distance * tangent);
  return { target, position: target.clone().addScaledVector(back, distance) };
}
