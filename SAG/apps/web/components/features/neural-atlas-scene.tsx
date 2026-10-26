"use client";

import * as React from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import { EffectComposer } from "three/examples/jsm/postprocessing/EffectComposer.js";
import { RenderPass } from "three/examples/jsm/postprocessing/RenderPass.js";
import { BokehPass } from "three/examples/jsm/postprocessing/BokehPass.js";
import { OutputPass } from "three/examples/jsm/postprocessing/OutputPass.js";
import type { NeuralCortexSceneProps, NeuralCortexSceneHandle } from "./neural-atlas-contract";
import type { UniverseSceneUnavailableReason } from "./universe-scene-contract";
import styles from "./neural-atlas.module.css";
import { ATLAS_FRAMING, atlasSeed as seed, atlasConnectionCurve, buildAtlasLayout, projectAtlasLabel } from "./neural-atlas-layout";
import { atlasNodeView } from "./neural-atlas-camera";

export interface NeuralAtlasSceneHandle extends NeuralCortexSceneHandle {
  zoomIn: () => void;
  zoomOut: () => void;
}
type Props = NeuralCortexSceneProps & { onUnavailable: (reason: UniverseSceneUnavailableReason) => void; keyboardHint: string };
const COPPER = "#b97b42";
const COLORS = ["#87977e", "#7f8c77", "#746d75", "#8f7e69", "#789887"];

function createGlow() {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 64;
  const context = canvas.getContext("2d");
  if (context) {
    const gradient = context.createRadialGradient(32, 32, 0, 32, 32, 32);
    gradient.addColorStop(0, "rgba(255,255,255,.9)");
    gradient.addColorStop(.18, "rgba(255,255,255,.4)");
    gradient.addColorStop(.5, "rgba(255,255,255,.08)");
    gradient.addColorStop(1, "rgba(255,255,255,0)");
    context.fillStyle = gradient;
    context.fillRect(0, 0, 64, 64);
  }
  return new THREE.CanvasTexture(canvas);
}

function disposeNetwork(group: THREE.Group) {
  const geometries = new Set<THREE.BufferGeometry>();
  const materials = new Set<THREE.Material>();
  group.traverse((object) => {
    if (object instanceof THREE.InstancedMesh) object.dispose();
    if (object instanceof THREE.Mesh || object instanceof THREE.Line || object instanceof THREE.Sprite) {
      if ("geometry" in object) geometries.add(object.geometry);
      (Array.isArray(object.material) ? object.material : [object.material]).forEach((material) => materials.add(material));
    }
  });
  group.clear();
  geometries.forEach((geometry) => geometry.dispose());
  materials.forEach((material) => material.dispose());
}

export const NeuralAtlasScene = React.forwardRef<NeuralAtlasSceneHandle, Props>(function NeuralAtlasScene(props, ref) {
  const containerRef = React.useRef<HTMLDivElement>(null);
  const labelsRef = React.useRef<HTMLDivElement>(null);
  const latest = React.useRef(props);
  latest.current = props;
  const [hovered, setHovered] = React.useState<string | null>(null);
  const positions = React.useRef(new Map<string, THREE.Vector3>());
  const fitKey = React.useRef("");
  const stateRef = React.useRef<{
    camera: THREE.PerspectiveCamera; controls: OrbitControls; network: THREE.Group;
    bokeh: BokehPass; glow: THREE.CanvasTexture; pickable: THREE.Mesh[]; ring: THREE.Mesh | null; dirty: boolean;
  } | null>(null);
  const flight = React.useRef<{ from: THREE.Vector3; to: THREE.Vector3; start: number; targetFrom: THREE.Vector3; targetTo: THREE.Vector3 } | null>(null);

  const layout = React.useMemo(() => buildAtlasLayout({ neurons: props.neurons, regions: props.regions, edges: props.edges, filterMode: props.filterMode }), [props.neurons, props.regions, props.edges, props.filterMode]);
  const layoutRef = React.useRef(layout);
  layoutRef.current = layout;

  const moveCamera = React.useCallback((position: THREE.Vector3, target: THREE.Vector3, animate = true) => {
    const state = stateRef.current;
    if (!state) return;
    const from = state.camera.position.clone(), targetFrom = state.controls.target.clone();
    // Clear leftover orbit damping before a scripted flight takes ownership.
    state.controls.enableDamping = false; state.controls.update();
    state.camera.position.copy(from); state.controls.target.copy(targetFrom); state.controls.update();
    state.controls.enableDamping = true;
    flight.current = null;
    state.dirty = true;
    if (animate && !latest.current.reducedMotion) flight.current = { from, to: position, start: performance.now(), targetFrom, targetTo: target };
    else { state.camera.position.copy(position); state.controls.target.copy(target); state.controls.update(); }
  }, []);

  const focusNode = React.useCallback((key: string, animate = true) => {
    const state = stateRef.current, host = containerRef.current, position = positions.current.get(key);
    if (!state || !host || !position || !host.clientWidth || !host.clientHeight) return;
    const bounds = host.getBoundingClientRect();
    const panel = host.closest("[data-universe-knowledge]")?.querySelector("[data-universe-detail-panel]")?.getBoundingClientRect();
    let right = bounds.width, bottom = bounds.height;
    if (panel && panel.top < bounds.bottom && panel.bottom > bounds.top) {
      if (panel.width < bounds.width * .75) right = Math.max(bounds.width * .35, panel.left - bounds.left - 24);
      else bottom = Math.max(bounds.height * .25, panel.top - bounds.top - 24);
    }
    const radius = Math.max(95, (layoutRef.current.points.get(key)?.radius ?? 9) * 12);
    const view = atlasNodeView(state.camera, position, radius, { width: bounds.width, height: bounds.height, right, bottom });
    moveCamera(view.position, view.target, animate);
  }, [moveCamera]);

  const fit = React.useCallback((animate = true) => {
    const state = stateRef.current;
    if (!state) return;
    state.dirty = true;
    if (!layoutRef.current.points.size) return;
    const box = new THREE.Box3().setFromPoints([
      ...[...layoutRef.current.points.values()].map((point) => point.position),
      ...layoutRef.current.beacons.map((beacon) => beacon.position),
    ]).expandByScalar(35);
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3()), tangent = Math.tan(THREE.MathUtils.degToRad(state.camera.fov / 2));
    const distance = Math.max(size.y / (2 * tangent), size.x / (2 * tangent * state.camera.aspect)) * 1.1 + size.z * .5;
    const destination = center.clone().add(new THREE.Vector3(0, 0, Math.max(440, distance)));
    moveCamera(destination, center, animate);
  }, [moveCamera]);

  React.useImperativeHandle(ref, () => ({
    focusAll: () => fit(),
    focusNode,
    zoomIn: () => { const state = stateRef.current; if (state) { flight.current = null; state.camera.position.sub(state.controls.target).multiplyScalar(.82).add(state.controls.target); state.controls.update(); state.dirty = true; } },
    zoomOut: () => { const state = stateRef.current; if (state) { flight.current = null; state.camera.position.sub(state.controls.target).multiplyScalar(1.22).add(state.controls.target); state.controls.update(); state.dirty = true; } },
  }), [fit, focusNode]);

  React.useEffect(() => {
    const host = containerRef.current;
    if (!host) return;
    let renderer: THREE.WebGLRenderer;
    try { renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" }); }
    catch { latest.current.onUnavailable("context-creation"); return; }
    const scene = new THREE.Scene();
    // Compensate for the filmic output transform so the canvas matches the app surface.
    scene.background = new THREE.Color("#1b2024");
    scene.fog = new THREE.FogExp2("#1b2024", .00035);
    const camera = new THREE.PerspectiveCamera(ATLAS_FRAMING.fov, 1, 1, 3500);
    camera.position.set(0, 0, ATLAS_FRAMING.distance);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.2;
    host.appendChild(renderer.domElement);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = .09;
    controls.rotateSpeed = .45;
    controls.minDistance = 120;
    controls.maxDistance = 2800;
    const pmrem = new THREE.PMREMGenerator(renderer), room = new RoomEnvironment();
    const environment = pmrem.fromScene(room, .04);
    scene.environment = environment.texture;
    scene.environmentIntensity = .9;
    room.dispose(); pmrem.dispose();
    scene.add(new THREE.AmbientLight("#e3ddd1", .4));
    const keyLight = new THREE.DirectionalLight("#fff2dc", 2.8);
    keyLight.position.set(-300, 400, 500); scene.add(keyLight);
    const fill = new THREE.DirectionalLight("#b0c6bd", .45);
    fill.position.set(80, -100, 120); scene.add(fill);
    const network = new THREE.Group(); scene.add(network);
    const glow = createGlow();
    const target = new THREE.WebGLRenderTarget(1, 1, { type: THREE.HalfFloatType, samples: Math.min(4, renderer.capabilities.maxSamples) });
    const composer = new EffectComposer(renderer, target), renderPass = new RenderPass(scene, camera);
    const bokeh = new BokehPass(scene, camera, { focus: 800, aperture: .000009, maxblur: .004 });
    const outputPass = new OutputPass();
    composer.addPass(renderPass); composer.addPass(bokeh); composer.addPass(outputPass);
    const state = { camera, controls, network, bokeh, glow, pickable: [] as THREE.Mesh[], ring: null as THREE.Mesh | null, dirty: true };
    stateRef.current = state;
    const interruptFlight = () => { flight.current = null; state.dirty = true; };
    controls.addEventListener("start", interruptFlight);
    const raycaster = new THREE.Raycaster(), pointer = new THREE.Vector2();
    let down = { x: 0, y: 0 }, pointerActive = false, frameId = 0, lastFrame = 0, visible = true;
    const hit = (event: PointerEvent) => {
      const bounds = renderer.domElement.getBoundingClientRect();
      pointer.set((event.clientX - bounds.left) / bounds.width * 2 - 1, -(event.clientY - bounds.top) / bounds.height * 2 + 1);
      raycaster.setFromCamera(pointer, camera);
      const intersection = raycaster.intersectObjects(state.pickable, false)[0];
      return intersection ? (intersection.instanceId === undefined ? intersection.object.userData.key : intersection.object.userData.keys?.[intersection.instanceId]) as string | undefined : undefined;
    };
    const pointerDown = (event: PointerEvent) => { down = { x: event.clientX, y: event.clientY }; pointerActive = true; flight.current = null; };
    const pointerMove = (event: PointerEvent) => {
      if (event.buttons) return;
      const key = hit(event) ?? null;
      renderer.domElement.style.cursor = key ? "pointer" : "grab";
      setHovered((previous) => previous === key ? previous : key);
    };
    const pointerUp = (event: PointerEvent) => {
      if (!pointerActive) return;
      pointerActive = false;
      if (Math.hypot(event.clientX - down.x, event.clientY - down.y) > 6) return;
      const key = hit(event), point = key ? layoutRef.current.points.get(key) : undefined;
      if (point) latest.current.onSelectNeuron(point.neuron); else latest.current.onClearSelection();
    };
    const pointerLeave = () => { pointerActive = false; setHovered(null); };
    const contextLost = (event: Event) => { event.preventDefault(); latest.current.onUnavailable("context-lost"); };
    renderer.domElement.addEventListener("pointerdown", pointerDown);
    renderer.domElement.addEventListener("pointermove", pointerMove);
    renderer.domElement.addEventListener("pointerup", pointerUp);
    renderer.domElement.addEventListener("pointerleave", pointerLeave);
    renderer.domElement.addEventListener("webglcontextlost", contextLost);
    const resize = new ResizeObserver(() => {
      const width = host.clientWidth, height = host.clientHeight;
      if (!width || !height) return;
      camera.aspect = width / height; camera.updateProjectionMatrix();
      renderer.setSize(width, height); composer.setSize(width, height);
      bokeh.materialBokeh.uniforms.aspect.value = camera.aspect;
      state.dirty = true;
      if (fitKey.current) {
        if (latest.current.selectedKey) focusNode(latest.current.selectedKey, false);
        else fit(false);
      }
    });
    resize.observe(host);
    const intersection = new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; });
    intersection.observe(host);
    const animate = (now: number) => {
      frameId = requestAnimationFrame(animate);
      if (!visible || document.hidden || now - lastFrame < 1000 / 60 - 1) return;
      lastFrame = now;
      if (flight.current) {
        const value = flight.current, progress = latest.current.reducedMotion ? 1 : Math.min(1, (now - value.start) / 900);
        const eased = progress * progress * (3 - 2 * progress);
        camera.position.lerpVectors(value.from, value.to, eased);
        controls.target.lerpVectors(value.targetFrom, value.targetTo, eased);
        if (progress === 1) flight.current = null;
      }
      const changed = controls.update();
      if (!changed && !flight.current && !state.dirty) return;
      state.dirty = false;
      camera.updateMatrixWorld();
      const pixelRadius = host.clientWidth < 600 ? 16 : 8;
      const tangent = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
      for (const object of state.pickable) {
        if (typeof object.userData.radius !== "number") continue;
        const depth = -object.position.clone().applyMatrix4(camera.matrixWorldInverse).z;
        object.scale.setScalar(Math.max(object.userData.radius, pixelRadius * 2 * Math.max(1, depth) * tangent / host.clientHeight));
      }
      const focus = latest.current.selectedKey ? layoutRef.current.points.get(latest.current.selectedKey) : undefined;
      bokeh.materialBokeh.uniforms.focus.value = focus ? -focus.position.clone().applyMatrix4(camera.matrixWorldInverse).z : camera.position.distanceTo(controls.target) - 100;
      if (state.ring) state.ring.quaternion.copy(camera.quaternion);
      composer.render();
      const placed: Array<{ x: number; y: number; width: number; height: number }> = [];
      for (const element of Array.from(labelsRef.current?.children ?? []) as HTMLElement[]) {
        const position = positions.current.get(element.dataset.key ?? "");
        if (!position) continue;
        const anchor = projectAtlasLabel(position, camera, host.clientWidth, host.clientHeight);
        const { x, y } = anchor;
        const width = element.offsetWidth, height = element.offsetHeight;
        const region = element.dataset.kind === "region";
        const left = Math.round(Math.max(8, Math.min(host.clientWidth - width - 8, x + (region ? -45 : 18))));
        let topEdge = y;
        if (region) {
          for (const point of layoutRef.current.points.values()) {
            if (point.group !== Number(element.dataset.group)) continue;
            const screen = projectAtlasLabel(point.position, camera, host.clientWidth, host.clientHeight);
            if (screen.visible) topEdge = Math.min(topEdge, screen.y);
          }
        }
        const top = Math.round(Math.max(8, Math.min(host.clientHeight - height - 8, region ? topEdge - height - 16 : y - height * .5 - 13)));
        const hidden = !anchor.visible || placed.some((rect) => left < rect.x + rect.width + 8 && left + width + 8 > rect.x && top < rect.y + rect.height + 8 && top + height + 8 > rect.y);
        element.style.visibility = hidden ? "hidden" : "visible";
        element.style.transform = `translate3d(${left}px,${top}px,0)`;
        element.dataset.anchorX = String(x);
        element.dataset.anchorY = String(y);
        if (region) {
          const dx = x - left - 2, dy = y - top - 14;
          element.style.setProperty("--leader-length", `${Math.hypot(dx, dy)}px`);
          element.style.setProperty("--leader-angle", `${Math.atan2(dy, dx)}rad`);
        }
        if (!hidden) placed.push({ x: left, y: top, width, height });
      }
    };
    frameId = requestAnimationFrame(animate);
    return () => {
      cancelAnimationFrame(frameId); resize.disconnect(); intersection.disconnect();
      renderer.domElement.removeEventListener("pointerdown", pointerDown);
      renderer.domElement.removeEventListener("pointermove", pointerMove);
      renderer.domElement.removeEventListener("pointerup", pointerUp);
      renderer.domElement.removeEventListener("pointerleave", pointerLeave);
      renderer.domElement.removeEventListener("webglcontextlost", contextLost);
      controls.removeEventListener("start", interruptFlight);
      controls.dispose(); disposeNetwork(network); glow.dispose(); environment.dispose();
      renderPass.dispose(); bokeh.dispose(); outputPass.dispose(); composer.dispose(); renderer.dispose();
      renderer.domElement.remove(); stateRef.current = null; fitKey.current = "";
    };
  }, [fit, focusNode]);

  React.useEffect(() => {
    const state = stateRef.current;
    if (!state) return;
    state.dirty = true;
    disposeNetwork(state.network); state.pickable = []; positions.current.clear();
    state.ring = null;
    const sphere = new THREE.SphereGeometry(1, 24, 20);
    const hitMaterial = new THREE.MeshBasicMaterial({ visible: false });
    const focusedGroup = props.selectedKey ? layout.points.get(props.selectedKey)?.group : 0;
    const colorFor = (group: number) => new THREE.Color(group === focusedGroup ? COPPER : COLORS[group % COLORS.length]);
    layout.points.forEach((point, key) => {
      const selected = key === props.selectedKey, active = point.group === focusedGroup || point.neuron.isMatched;
      const color = (selected ? new THREE.Color("#e0a264") : colorFor(point.group)).offsetHSL(0, 0, (seed(key + ":pearl") - .5) * .12);
      const material = new THREE.MeshPhysicalMaterial({ color, metalness: .35, roughness: .19, clearcoat: 1, clearcoatRoughness: .1, envMapIntensity: .72, emissive: color, emissiveIntensity: selected ? .6 : active ? .025 : .008 });
      const mesh = new THREE.Mesh(sphere, material), radius = selected ? Math.max(10, point.radius * 1.1) : point.radius;
      mesh.scale.setScalar(radius); mesh.position.copy(point.position);
      state.network.add(mesh); positions.current.set(key, point.position);
      const hitTarget = new THREE.Mesh(sphere, hitMaterial);
      hitTarget.position.copy(point.position); hitTarget.scale.setScalar(Math.max(radius, 7)); hitTarget.userData.key = key; hitTarget.userData.radius = radius;
      state.network.add(hitTarget); state.pickable.push(hitTarget);
      if (selected || point.radius > 5) {
        const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: state.glow, color, transparent: true, opacity: selected ? .9 : active ? .11 : .045, depthWrite: false, blending: THREE.AdditiveBlending }));
        sprite.position.copy(point.position); sprite.scale.setScalar(radius * (selected ? 8 : 5)); state.network.add(sprite);
      }
      if (selected) {
        state.ring = new THREE.Mesh(new THREE.TorusGeometry(radius * 1.1, .42, 8, 64), new THREE.MeshBasicMaterial({ color: "#ffe3bd" }));
        state.ring.position.copy(point.position); state.network.add(state.ring);
      }
    });
    if (!layout.points.size) { sphere.dispose(); hitMaterial.dispose(); }
    const beaconGeometry = new THREE.SphereGeometry(1, 10, 8);
    const beaconMesh = new THREE.InstancedMesh(beaconGeometry, new THREE.MeshStandardMaterial({ color: "#ffffff", metalness: .36, roughness: .33 }), layout.beacons.length);
    const matrix = new THREE.Matrix4(), quaternion = new THREE.Quaternion(), evidencePositions: number[] = [], evidenceColors: number[] = [];
    const parentKeys: string[] = [];
    layout.beacons.forEach((beacon, index) => {
      matrix.compose(beacon.position, quaternion, new THREE.Vector3().setScalar(beacon.radius));
      beaconMesh.setMatrixAt(index, matrix); beaconMesh.setColorAt(index, colorFor(beacon.group).offsetHSL(0, -.05, .09)); parentKeys.push(beacon.parentKey);
      const parent = layout.points.get(beacon.parentKey)!;
      const samples = atlasConnectionCurve(parent.position, beacon.position, `${beacon.parentKey}:${index}`, false).getPoints(10);
      const color = colorFor(beacon.group).multiplyScalar(beacon.group === focusedGroup ? 1 : .6);
      for (let sample = 1; sample < samples.length; sample++) {
        evidencePositions.push(...samples[sample - 1].toArray(), ...samples[sample].toArray());
        evidenceColors.push(...color.toArray(), ...color.toArray());
      }
    });
    beaconMesh.userData.keys = parentKeys;
    if (layout.beacons.length) { state.network.add(beaconMesh); state.pickable.push(beaconMesh); }
    else { beaconGeometry.dispose(); beaconMesh.material.dispose(); }
    const evidenceGeometry = new THREE.BufferGeometry();
    evidenceGeometry.setAttribute("position", new THREE.Float32BufferAttribute(evidencePositions, 3));
    evidenceGeometry.setAttribute("color", new THREE.Float32BufferAttribute(evidenceColors, 3));
    state.network.add(new THREE.LineSegments(evidenceGeometry, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: .3, depthWrite: false })));
    layout.centers.forEach((position, key) => positions.current.set(key, position));
    layout.labels.forEach((label) => positions.current.set(label.region.key, label.position));
    const degree = new Map<string, number>(), crossCount = new Map<string, number>();
    const edgeBuckets = new Map<string, { coordinates: number[]; active: boolean; selected: boolean; cross: boolean }>();
    for (const edge of [...layout.edges].sort((a, b) => b.weight - a.weight || a.id.localeCompare(b.id))) {
      const source = layout.points.get(edge.sourceKey)!, target = layout.points.get(edge.targetKey)!;
      const selected = edge.sourceKey === props.selectedKey || edge.targetKey === props.selectedKey;
      const cross = source.group !== target.group, pair = [source.group, target.group].sort((a, b) => a - b).join(":");
      // Limit overview strokes; the inspector retains every source relation.
      if (!selected && ((degree.get(edge.sourceKey) ?? 0) >= 7 || (degree.get(edge.targetKey) ?? 0) >= 7 || (cross && (crossCount.get(pair) ?? 0) >= 8))) continue;
      degree.set(edge.sourceKey, (degree.get(edge.sourceKey) ?? 0) + 1); degree.set(edge.targetKey, (degree.get(edge.targetKey) ?? 0) + 1);
      if (cross) crossCount.set(pair, (crossCount.get(pair) ?? 0) + 1);
      const active = source.group === focusedGroup || target.group === focusedGroup;
      const bucketKey = `${selected}:${active}:${cross}`;
      const bucket = edgeBuckets.get(bucketKey) ?? { coordinates: [], active, selected, cross };
      const samples = atlasConnectionCurve(source.position, target.position, edge.id, cross, source.group, target.group).getPoints(cross ? 40 : 20);
      for (let sample = 1; sample < samples.length; sample++) bucket.coordinates.push(...samples[sample - 1].toArray(), ...samples[sample].toArray());
      edgeBuckets.set(bucketKey, bucket);
    }
    edgeBuckets.forEach(({ coordinates, active, selected, cross }) => {
      const geometry = new THREE.BufferGeometry(); geometry.setAttribute("position", new THREE.Float32BufferAttribute(coordinates, 3));
      const material = new THREE.LineBasicMaterial({ color: active ? "#d3a16d" : "#829681", transparent: true, opacity: selected ? .42 : active ? (cross ? .18 : .22) : .15, depthWrite: false });
      state.network.add(new THREE.LineSegments(geometry, material));
    });
    if (layout.key !== fitKey.current && (layout.points.size || layout.labels.length)) {
      const animate = Boolean(fitKey.current);
      fitKey.current = layout.key; fit(animate);
    }
  }, [layout, props.selectedKey, fit]);

  React.useEffect(() => { if (stateRef.current) stateRef.current.dirty = true; }, [hovered]);
  React.useEffect(() => setHovered(null), [props.selectedKey]);
  React.useEffect(() => {
    if (props.selectedKey) focusNode(props.selectedKey);
  }, [props.selectedKey, layout.key, focusNode]);

  const focus = hovered ? layout.points.get(hovered) : props.selectedKey ? layout.points.get(props.selectedKey) : undefined;
  return <div className={styles.scene}>
    <div ref={containerRef} className={styles.sceneCanvas} data-atlas-units={layout.points.size} data-atlas-evidence={layout.beacons.length} tabIndex={0} role="group" aria-label={props.keyboardHint}
      onKeyDown={(event) => {
        const points = [...layout.points.values()];
        if (!points.length) return;
        const current = points.findIndex((point) => point.neuron.key === (hovered ?? props.selectedKey));
        const direction = ["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : ["ArrowLeft", "ArrowUp"].includes(event.key) ? -1 : 0;
        if (direction) { event.preventDefault(); setHovered(points[(Math.max(0, current) + direction + points.length) % points.length].neuron.key); }
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); props.onSelectNeuron(points[Math.max(0, current)].neuron); }
      }} />
    <div ref={labelsRef} className={styles.sceneLabels}>
      {focus && <button type="button" data-key={focus.neuron.key} className={styles.neuronLabel} onClick={() => props.onSelectNeuron(focus.neuron)}>{focus.neuron.cleanLabel}</button>}
      {layout.labels.slice(0, 7).map((label) => <button type="button" key={label.region.key} data-key={label.region.key} data-kind="region" data-group={label.group} className={styles.regionLabel} data-active={label.group === (props.selectedKey ? layout.points.get(props.selectedKey)?.group : 0)} onClick={() => props.onSelectRegion(label.region)}>{label.region.label}</button>)}
    </div>
  </div>;
});
