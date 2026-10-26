"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { useReducedMotion } from "motion/react";
import {
  ArrowLeft, Brain, ChevronLeft, ChevronRight, FileText, Focus,
  Layers3, Loader2, Orbit, RefreshCw, Search, X, Minus, Plus,
} from "lucide-react";

import { api, ApiError } from "@/lib/api";
import type {
  UniverseActivation, UniverseKnowledgeDetail, UniverseKnowledgeEdge, UniverseKnowledgeMatch,
  UniverseKnowledgePage, UniverseKnowledgeSynapse, UniverseKnowledgeUnit, UniverseManifest,
  UniverseTreeNode,
} from "@/lib/types";
import {
  dispatchUniverseInteraction, dispatchUniverseView,
  UNIVERSE_ACTIVATE_EVENT, UNIVERSE_FOCUS_EVENT, UNIVERSE_RESET_EVENT,
  UNIVERSE_SOURCE_FOCUS_EVENT,
} from "@/lib/universe-events";
import {
  findKnowledgeCluster, groupUnitsByDocument, knowledgeClusterKey, knowledgeClusterTrail,
  knowledgeUnitKey, type KnowledgeCluster,
} from "@/lib/universe-knowledge-projection";
import { detectUniverseWebGLCapability } from "@/lib/universe-webgl-capability";
import { useUniverseViewPreferences } from "@/lib/universe-view-preferences";
import { cn } from "@/lib/utils";
import { useApp } from "@/components/features/app-shell";
import { MarkdownContent } from "./markdown-content";
import { knowledgeDocumentTitle, knowledgePassage, knowledgeUnitTitle, knowledgeRegionTitle } from "@/lib/knowledge-presentation";
import { NeuralAtlasScene, type NeuralAtlasSceneHandle } from "./neural-atlas-scene";
import type {
  NeuralCortexEdgeItem,
  NeuralCortexNeuronItem,
  NeuralCortexRegionItem,
} from "@/components/features/neural-atlas-contract";
import type { UniverseSceneUnavailableReason } from "@/components/features/universe-scene-contract";
import { Button } from "@/components/ui/button";
import styles from "./neural-atlas.module.css";

const EMPTY_TREES: UniverseManifest["trees"] = [];

function stripMarkdownNoise(raw: string): string {
  return raw
    .replace(/\|[\s\-:]+\|/g, " ")
    .replace(/\|/g, " · ")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/\*\*/g, "")
    .replace(/__/g, "")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/`/g, "")
    .replace(/^[\s*\-#>•]+/, "")
    .replace(/\s+/g, " ")
    .trim();
}

function humanizeDocName(docName: string): string {
  return knowledgeDocumentTitle(docName);
}

function cleanNeuronLabel(unit: UniverseKnowledgeUnit): string {
  return knowledgeUnitTitle(unit);
}

const CRYPTIC_CLUSTER_LABELS = new Set([
  "DOC", "ALL", "GRP", "HTTP", "SKU", "AUTHORIZED", "HMAC", "/P1",
  "0 UNITS", "2 HOURS", "30 DAYS", "72 HOURS", "STAFF", "PENDING", "ADR-001", "NSC",
]);

const SEMANTIC_LOBE_FALLBACKS: Record<string, string> = {
  HTTP: "API & transport",
  SKU: "Catalog & inventory",
  AUTHORIZED: "Access & governance",
  HMAC: "Cryptographic authentication",
  DOC: "Documents",
  ALL: "Shared knowledge",
  GRP: "Group policies",
};

function formatCorticalRegionLabel(
  node: UniverseTreeNode,
  subtreeUnits: UniverseKnowledgeUnit[],
  fallbackLabel: string,
): string {
  const raw = stripMarkdownNoise(node.label || "");
  if (!raw) return fallbackLabel;
  if (!node.parent_id && raw.toUpperCase() === "DOC") {
    return fallbackLabel;
  }
  if (!node.is_leaf && subtreeUnits.length) {
    const topic = knowledgeRegionTitle(subtreeUnits, raw);
    if (topic) return topic;
  }
  const upper = raw.toUpperCase();
  const isCryptic = raw.length <= 6 || CRYPTIC_CLUSTER_LABELS.has(upper) || /^\d+\s+\w+$/i.test(raw);
  if (!isCryptic) return raw;

  if (subtreeUnits.length > 0) {
    const docCounts = new Map<string, number>();
    subtreeUnits.forEach((u) => {
      const name = humanizeDocName(u.document_name);
      docCounts.set(name, (docCounts.get(name) ?? 0) + 1);
    });
    const topDoc = Array.from(docCounts.entries()).sort((a, b) => b[1] - a[1])[0]?.[0];
    if (topDoc) {
      return topDoc;
    }
  }

  if (SEMANTIC_LOBE_FALLBACKS[upper]) {
    return SEMANTIC_LOBE_FALLBACKS[upper];
  }

  const topEnt = node.top_entities?.find((e) => {
    const trimmed = e.trim();
    const eu = trimmed.toUpperCase();
    return (
      eu !== upper &&
      trimmed.length > 4 &&
      trimmed !== eu &&
      !CRYPTIC_CLUSTER_LABELS.has(eu) &&
      !/^\d+\s+\w+$/i.test(trimmed)
    );
  });
  if (topEnt) {
    return stripMarkdownNoise(topEnt);
  }
  return raw;
}

export function KnowledgeRoutingUniverse({ interactive = true }: { interactive?: boolean }) {
  const t = useTranslations("KnowledgeUniverse.knowledge");
  const webglText = useTranslations("KnowledgeUniverse.webgl");
  const reducedMotion = useReducedMotion();
  const { preferences } = useUniverseViewPreferences();
  const { exitExploreMode } = useApp();
  const graphRef = React.useRef<NeuralAtlasSceneHandle>(null);
  const searchRef = React.useRef<HTMLInputElement>(null);
  const [searchQuery, setSearchQuery] = React.useState("");

  const [manifest, setManifest] = React.useState<UniverseManifest | null>(null);
  const [revision, setRevision] = React.useState(0);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState("");
  const [active, setActive] = React.useState<KnowledgeCluster | null>(null);
  const [page, setPage] = React.useState<UniverseKnowledgePage | null>(null);
  const [ambientPages, setAmbientPages] = React.useState<UniverseKnowledgePage[]>([]);
  const [ambientLinks, setAmbientLinks] = React.useState<UniverseKnowledgeEdge[]>([]);
  const [pageLoading, setPageLoading] = React.useState(false);
  const [cursors, setCursors] = React.useState<Array<string | undefined>>([undefined]);
  const [pageIndex, setPageIndex] = React.useState(0);
  const [selected, setSelected] = React.useState<{
    unitId: string;
    projectId: string;
    treeVersion: string;
    nodeId: string;
  } | null>(null);
  const [detail, setDetail] = React.useState<UniverseKnowledgeDetail | null>(null);
  const [detailLoading, setDetailLoading] = React.useState(false);
  const [showList, setShowList] = React.useState(false);
  const [activation, setActivation] = React.useState<UniverseActivation | null>(null);
  const [matches, setMatches] = React.useState<UniverseKnowledgeMatch[]>([]);
  const [resolving, setResolving] = React.useState(false);
  const [sceneFailure, setSceneFailure] = React.useState<UniverseSceneUnavailableReason | "api-unavailable" | null>(null);
  const [sceneAttempt, setSceneAttempt] = React.useState(0);
  const pendingFocus = React.useRef<string | null>(null);
  const interactiveRef = React.useRef(interactive);
  interactiveRef.current = interactive;

  const trees = manifest?.trees ?? EMPTY_TREES;
  const sourceProjection = trees.some((tree) => tree.projection === "source");
  const current = React.useMemo(() => findKnowledgeCluster(trees, active), [trees, active]);
  const activeKey = active ? knowledgeClusterKey(active) : "overview";
  const pageSize = Math.min(50, preferences.eventWindowSize);

  const clearSelection = React.useCallback(() => {
    setSelected(null);
    setDetail(null);
    setDetailLoading(false);
  }, []);

  React.useEffect(() => {
    if (!interactive) return;
    const onSearchShortcut = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "k") return;
      event.preventDefault(); event.stopImmediatePropagation(); clearSelection(); setShowList(true);
    };
    window.addEventListener("keydown", onSearchShortcut, true);
    return () => window.removeEventListener("keydown", onSearchShortcut, true);
  }, [interactive, clearSelection]);

  const navigate = React.useCallback((target: KnowledgeCluster | null, focusUnit?: string) => {
    if (target && !findKnowledgeCluster(trees, target)) {
      setError(t("snapshotChanged"));
      return;
    }
    pendingFocus.current = focusUnit ?? null;
    setActive(target ? { project_id: target.project_id, tree_version: target.tree_version, node_id: target.node_id } : null);
    setPage(null);
    setPageLoading(false);
    setSelected(null);
    setDetail(null);
    setDetailLoading(false);
    setCursors([undefined]);
    setPageIndex(0);
    setError("");
  }, [trees, t]);

  const onFailure = React.useCallback((reason: unknown) => {
    if (reason instanceof ApiError && reason.code === "aborted") return;
    if (reason instanceof DOMException && reason.name === "AbortError") return;
    setError(reason instanceof ApiError ? reason.message : t("loadFailed"));
    if (reason instanceof ApiError && [401, 403, 404, 409].includes(reason.status)) {
      setPage(null);
      setAmbientPages([]);
      setAmbientLinks([]);
      setDetail(null);
      setSelected(null);
      setMatches([]);
      setManifest(null);
    }
  }, [t]);

  React.useEffect(() => {
    if (detectUniverseWebGLCapability(window) !== "available") setSceneFailure("api-unavailable");
  }, []);

  React.useEffect(() => {
    if (!interactive) {
      setManifest(null);
      setPage(null);
      setAmbientPages([]);
      setAmbientLinks([]);
      setDetail(null);
      setSelected(null);
      setPageLoading(false);
      setDetailLoading(false);
      setResolving(false);
      setMatches([]);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError("");
    void api.universeKnowledgeOverview(controller.signal).then((value) => {
      if (controller.signal.aborted) return;
      setManifest(value);
      const roots = value.trees.flatMap((tree) => tree.nodes.filter((node) => !node.parent_id).map((node) => ({ tree, node })));
      if (roots.length === 1 && roots[0].node.is_leaf) {
        const { tree, node } = roots[0];
        setActive({ project_id: tree.project_id, tree_version: tree.tree_version, node_id: node.id });
      } else {
        setActive(null);
      }
      setCursors([undefined]);
      setPageIndex(0);
    }).catch((reason) => { if (!controller.signal.aborted) onFailure(reason); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [interactive, revision, onFailure]);

  const refresh = React.useCallback(() => {
    clearSelection();
    setPage(null);
    setAmbientPages([]);
    setAmbientLinks([]);
    setManifest(null);
    setActive(null);
    setMatches([]);
    setRevision((value) => value + 1);
  }, [clearSelection]);

  React.useEffect(() => {
    if (!interactive || !active || !current?.node.is_leaf) return;
    const controller = new AbortController();
    setPageLoading(true);
    setPage(null);
    setError("");
    void api.universeKnowledgeCluster({
      ...active, limit: pageSize, cursor: cursors[pageIndex],
    }, controller.signal).then((value) => {
      if (controller.signal.aborted) return;
      if (value.project_id !== active.project_id || value.tree_version !== active.tree_version || value.node_id !== active.node_id) {
        onFailure(new ApiError(409, "snapshot_changed", t("snapshotChanged")));
        return;
      }
      setPage(value);
    }).catch((reason) => { if (!controller.signal.aborted) onFailure(reason); })
      .finally(() => { if (!controller.signal.aborted) setPageLoading(false); });
    return () => controller.abort();
  }, [interactive, active, current?.node.is_leaf, pageSize, cursors, pageIndex, onFailure, t]);

  // Load the visible routing regions, so each page retains its genuine internal edges.
  React.useEffect(() => {
    if (!interactive || !trees.length || (active && current?.node.is_leaf)) return;
    const controller = new AbortController();
    const targets = trees.flatMap((tree) => {
      if (active && (tree.project_id !== active.project_id || tree.tree_version !== active.tree_version)) return [];
      const roots = active ? [active.node_id] : tree.nodes.filter((node) => !node.parent_id).map((node) => node.id);
      const children = tree.nodes.filter((node) => node.parent_id && roots.includes(node.parent_id));
      const visible = children.length ? children : tree.nodes.filter((node) => roots.includes(node.id));
      return visible.filter((node) => node.unit_count > 0).map((node) => ({ project_id: tree.project_id, tree_version: tree.tree_version, node_id: node.id, unit_count: node.unit_count }));
    }).sort((a, b) => b.unit_count - a.unit_count).slice(0, 12);
    // shortcut: overview shows up to 12 regions × 50 units; drill into a region for its paged contents.
    setPageLoading(true);
    setAmbientPages([]);
    setAmbientLinks([]);
    void (async () => {
      const pages: UniverseKnowledgePage[] = [];
      for (let offset = 0; offset < targets.length; offset += 4) {
        if (controller.signal.aborted) return;
        const batch = await Promise.all(targets.slice(offset, offset + 4).map((target) =>
          api.universeKnowledgeCluster({ project_id: target.project_id, tree_version: target.tree_version, node_id: target.node_id, limit: 50 }, controller.signal).catch((reason) => {
            if (!controller.signal.aborted) onFailure(reason);
            return null;
          }),
        ));
        pages.push(...batch.filter((item): item is UniverseKnowledgePage => item !== null));
      }
      // A parent page supplies real connections between its child regions.
      const scopes = trees.flatMap((tree) => {
        if (active && (tree.project_id !== active.project_id || tree.tree_version !== active.tree_version)) return [];
        return tree.nodes.filter((node) => active ? node.id === active.node_id : !node.parent_id)
          .filter((node) => !node.is_leaf).map((node) => ({ project_id: tree.project_id, tree_version: tree.tree_version, node_id: node.id }));
      }).slice(0, 12);
      const links: UniverseKnowledgeEdge[] = [];
      // shortcut: cross-region overview links come from each parent's first 50 units; details expose all relations.
      for (let offset = 0; offset < scopes.length; offset += 4) {
        if (controller.signal.aborted) return;
        const batch = await Promise.all(scopes.slice(offset, offset + 4).map((scope) =>
          api.universeKnowledgeCluster({ ...scope, limit: 50 }, controller.signal).catch((reason) => {
            if (!controller.signal.aborted) onFailure(reason);
            return null;
          }),
        ));
        batch.forEach((value) => { if (value) links.push(...value.edges); });
      }
      if (!controller.signal.aborted) { setAmbientPages(pages); setAmbientLinks(links); }
    })().finally(() => { if (!controller.signal.aborted) setPageLoading(false); });
    return () => controller.abort();
  }, [interactive, trees, active, current?.node.is_leaf, onFailure]);

  React.useEffect(() => {
    setCursors([undefined]);
    setPageIndex(0);
  }, [pageSize]);

  React.useEffect(() => {
    if (!interactive || !selected) return;
    const controller = new AbortController();
    setDetailLoading(true);
    setDetail(null);
    void api.universeKnowledgeUnit(selected.unitId, selected.projectId, selected.treeVersion, controller.signal).then((value) => {
      if (controller.signal.aborted) return;
      setDetail(value);
    }).catch((reason) => { if (!controller.signal.aborted) onFailure(reason); })
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [interactive, selected, onFailure]);

  const activePages = React.useMemo(
    () => (current?.node.is_leaf ? (page ? [page] : []) : ambientPages),
    [current?.node.is_leaf, page, ambientPages],
  );

  const allVisibleUnits = React.useMemo(() => {
    const seen = new Set<string>();
    const out: Array<{ unit: UniverseKnowledgeUnit; ref: KnowledgeCluster }> = [];
    for (const p of activePages) {
      const ref: KnowledgeCluster = {
        project_id: p.project_id,
        tree_version: p.tree_version,
        node_id: p.node_id,
      };
      for (const u of p.units) {
        if (!seen.has(u.id)) {
          seen.add(u.id);
          out.push({ unit: u, ref });
        }
      }
    }
    return out;
  }, [activePages]);

  const allVisibleEdges = React.useMemo(() => {
    const out: UniverseKnowledgeEdge[] = [];
    const seen = new Set<string>();
    const edges = activePages.flatMap((value) => value.edges);
    if (!current?.node.is_leaf) edges.push(...ambientLinks);
    for (const edge of edges) {
      const key = `${edge.source}~${edge.target}`;
      if (!seen.has(key)) {
        seen.add(key);
        out.push(edge);
      }
    }
    return out;
  }, [activePages, ambientLinks, current?.node.is_leaf]);

  React.useEffect(() => {
    const controller = new AbortController();
    const onActivation = (event: Event) => {
      const value = (event as CustomEvent<UniverseActivation>).detail;
      if (value) { setActivation(value); setMatches([]); }
    };
    const onReset = () => { setActivation(null); setMatches([]); navigate(null); };
    const onFocus = (event: Event) => {
      const value = (event as CustomEvent<{ id?: string; source_id?: string }>).detail;
      const found = allVisibleUnits.find(({ unit }) => unit.block_id === value?.id || unit.id === value?.id);
      if (found) {
        const key = knowledgeUnitKey(found.ref, found.unit.id);
        setSelected({
          unitId: found.unit.id,
          projectId: found.ref.project_id,
          treeVersion: found.ref.tree_version,
          nodeId: found.ref.node_id,
        });
        graphRef.current?.focusNode(key);
      }
    };
    const onSourceFocus = (event: Event) => {
      const value = (event as CustomEvent<{ source_id?: string }>).detail;
      const found = allVisibleUnits.find(({ unit }) => unit.source_id === value?.source_id);
      if (found) {
        graphRef.current?.focusNode(knowledgeUnitKey(found.ref, found.unit.id));
      } else if (value?.source_id && interactiveRef.current) {
        void api.universeKnowledgeResolve([{ source_id: value.source_id }], controller.signal).then((result) => {
          const target = result.matches[0];
          if (!controller.signal.aborted && target) navigate(target, target.unit_ids[0]);
        }).catch((reason) => { if (!controller.signal.aborted) onFailure(reason); });
      }
    };
    window.addEventListener(UNIVERSE_ACTIVATE_EVENT, onActivation);
    window.addEventListener(UNIVERSE_RESET_EVENT, onReset);
    window.addEventListener(UNIVERSE_FOCUS_EVENT, onFocus);
    window.addEventListener(UNIVERSE_SOURCE_FOCUS_EVENT, onSourceFocus);
    return () => {
      controller.abort();
      window.removeEventListener(UNIVERSE_ACTIVATE_EVENT, onActivation);
      window.removeEventListener(UNIVERSE_RESET_EVENT, onReset);
      window.removeEventListener(UNIVERSE_FOCUS_EVENT, onFocus);
      window.removeEventListener(UNIVERSE_SOURCE_FOCUS_EVENT, onSourceFocus);
    };
  }, [navigate, allVisibleUnits, onFailure]);

  React.useEffect(() => {
    if (!interactive || !manifest || !activation?.evidence_refs?.length) return;
    const controller = new AbortController();
    setResolving(true);
    void api.universeKnowledgeResolve(activation.evidence_refs.slice(0, 100), controller.signal).then((value) => {
      if (!controller.signal.aborted) setMatches(value.matches);
    }).catch((reason) => { if (!controller.signal.aborted) onFailure(reason); })
      .finally(() => { if (!controller.signal.aborted) setResolving(false); });
    return () => controller.abort();
  }, [interactive, manifest, activation, onFailure]);

  const routedNodes = React.useMemo(() => {
    const selectedNodes = new Set<string>();
    for (const route of activation?.tree_routes ?? []) {
      const tree = trees.find((item) => item.project_id === route.project_id && item.tree_version === route.tree_version);
      if (!tree) continue;
      const byId = new Map(tree.nodes.map((node) => [node.id, node]));
      for (const id of route.node_ids) {
        let node = byId.get(id);
        const seen = new Set<string>();
        while (node && !seen.has(node.id)) {
          seen.add(node.id);
          selectedNodes.add(knowledgeClusterKey({ project_id: tree.project_id, tree_version: tree.tree_version, node_id: node.id }));
          node = node.parent_id ? byId.get(node.parent_id) : undefined;
        }
      }
    }
    return selectedNodes;
  }, [activation, trees]);

  const matchedClusters = React.useMemo(() => new Set(matches.flatMap((match) =>
    knowledgeClusterTrail(trees, match).map((node) => knowledgeClusterKey({ ...match, node_id: node.id })),
  )), [matches, trees]);

  // Map node_id -> all units inside that node or its descendants so we can give every cortical region a meaningful title
  const unitsByNodeSubtree = React.useMemo(() => {
    const map = new Map<string, UniverseKnowledgeUnit[]>();
    const directByNode = new Map<string, UniverseKnowledgeUnit[]>();
    allVisibleUnits.forEach(({ unit, ref }) => {
      const list = directByNode.get(ref.node_id) ?? [];
      list.push(unit);
      directByNode.set(ref.node_id, list);
    });
    for (const tree of trees) {
      const childrenByParent = new Map<string, UniverseTreeNode[]>();
      tree.nodes.forEach((n) => {
        if (n.parent_id) {
          const list = childrenByParent.get(n.parent_id) ?? [];
          list.push(n);
          childrenByParent.set(n.parent_id, list);
        }
      });
      const collect = (nodeId: string, visited = new Set<string>()): UniverseKnowledgeUnit[] => {
        if (visited.has(nodeId)) return [];
        visited.add(nodeId);
        const out = [...(directByNode.get(nodeId) ?? [])];
        for (const child of childrenByParent.get(nodeId) ?? []) {
          out.push(...collect(child.id, visited));
        }
        return out;
      };
      tree.nodes.forEach((n) => {
        map.set(n.id, collect(n.id));
      });
    }
    return map;
  }, [trees, allVisibleUnits]);

  const subtreeRegions = React.useMemo<NeuralCortexRegionItem[]>(() => {
    const items: NeuralCortexRegionItem[] = [];
    for (const tree of trees) {
      if (active && (tree.project_id !== active.project_id || tree.tree_version !== active.tree_version)) continue;
      const byParent = new Map<string, UniverseTreeNode[]>();
      tree.nodes.forEach((n) => {
        if (n.parent_id) {
          const list = byParent.get(n.parent_id) ?? [];
          list.push(n);
          byParent.set(n.parent_id, list);
        }
      });

      const addSubtree = (roots: UniverseTreeNode[]) => {
        const queue = [...roots];
        const visited = new Set<string>();
        let idx = 0;
        while (queue.length > 0) {
          const node = queue.shift()!;
          if (visited.has(node.id)) continue;
          visited.add(node.id);
          idx += 1;
          const ref: KnowledgeCluster = {
            project_id: tree.project_id,
            tree_version: tree.tree_version,
            node_id: node.id,
          };
          const key = knowledgeClusterKey(ref);
          const fallbackLabel = t("clusterFallback", { index: idx });
          const humanLabel = formatCorticalRegionLabel(
            node,
            unitsByNodeSubtree.get(node.id) ?? [],
            fallbackLabel,
          );
          items.push({
            key,
            project_id: tree.project_id,
            tree_version: tree.tree_version,
            node,
            label: humanLabel,
            stats: t("clusterStats", { units: node.unit_count, sources: node.source_count }),
            isActive: activeKey === key,
            isRouted: routedNodes.has(key),
            isMatched: matchedClusters.has(key),
          });
          const children = byParent.get(node.id) ?? [];
          queue.push(...children);
        }
      };

      if (active) {
        const start = tree.nodes.find((n) => n.id === active.node_id);
        if (start) addSubtree([start]);
      } else {
        addSubtree(tree.nodes.filter((n) => !n.parent_id));
      }
    }
    return items;
  }, [trees, active, activeKey, routedNodes, matchedClusters, unitsByNodeSubtree, t]);

  const clusterChoices = React.useMemo(() => {
    const choices = current && active
      ? [{ tree: current.tree, node: current.node }, ...current.tree.nodes
        .filter((node) => node.parent_id === active.node_id).map((node) => ({ tree: current.tree, node }))]
      : trees.flatMap((tree) => tree.nodes.filter((node) => !node.parent_id).map((node) => ({ tree, node })));
    return choices.map(({ tree, node }, index) => ({
      ref: { project_id: tree.project_id, tree_version: tree.tree_version, node_id: node.id },
      node,
      label: formatCorticalRegionLabel(
        node,
        unitsByNodeSubtree.get(node.id) ?? [],
        t("clusterFallback", { index: index + 1 }),
      ),
      stats: t("clusterStats", { units: node.unit_count, sources: node.source_count }),
    }));
  }, [current, active, trees, unitsByNodeSubtree, t]);

  const selectedSceneId = React.useMemo(
    () => (selected ? knowledgeUnitKey({ project_id: selected.projectId, tree_version: selected.treeVersion, node_id: selected.nodeId }, selected.unitId) : null),
    [selected],
  );

  const { cortexNeurons, cortexEdges } = React.useMemo(() => {
    const matchedUnitIds = new Set(matches.flatMap((m) => m.unit_ids));
    const unitKeyById = new Map<string, string>();
    const unitById = new Map<string, UniverseKnowledgeUnit>();

    const neuronItems: NeuralCortexNeuronItem[] = allVisibleUnits.map(({ unit, ref }) => {
      const clusterKey = knowledgeClusterKey(ref);
      const key = knowledgeUnitKey(ref, unit.id);
      unitKeyById.set(unit.id, key);
      unitById.set(unit.id, unit);
      return {
        key,
        clusterKey,
        unit,
        cleanLabel: cleanNeuronLabel(unit),
        isSelected: selected?.unitId === unit.id,
        isMatched: matchedUnitIds.has(unit.id),
      };
    });

    const edgeItems: NeuralCortexEdgeItem[] = [];
    const seen = new Set<string>();
    for (const edge of allVisibleEdges) {
      const sourceKey = unitKeyById.get(edge.source), targetKey = unitKeyById.get(edge.target);
      if (!sourceKey || !targetKey) continue;
      const id = [sourceKey, targetKey].sort().join("~");
      if (seen.has(id)) continue;
      seen.add(id);
      const source = unitById.get(edge.source), target = unitById.get(edge.target);
      edgeItems.push({ id, sourceKey, targetKey,
        kind: edge.cross_document || source?.document_id !== target?.document_id ? "cross_doc_axon" : "local_dendrite",
        weight: edge.weight, sharedEntities: edge.shared_entities,
      });
    }

    if (detail && selected) {
      const sourceKey = unitKeyById.get(detail.id);
      for (const synapse of detail.synapses ?? []) {
        const targetKey = unitKeyById.get(synapse.target_unit_id);
        if (!sourceKey || !targetKey) continue;
        const id = [sourceKey, targetKey].sort().join("~");
        if (seen.has(id)) continue;
        seen.add(id);
        edgeItems.push({ id, sourceKey, targetKey, kind: synapse.cross_document ? "cross_doc_axon" : "local_dendrite", weight: synapse.weight, sharedEntities: synapse.shared_entities });
      }
    }
    return { cortexNeurons: neuronItems, cortexEdges: edgeItems };
  }, [allVisibleUnits, allVisibleEdges, matches, selected, detail]);

  const trail = knowledgeClusterTrail(trees, active);

  React.useEffect(() => {
    if (!interactive) return;
    if (pendingFocus.current) {
      const target = allVisibleUnits.find(({ unit }) => unit.id === pendingFocus.current);
      if (target) {
        const key = knowledgeUnitKey(target.ref, target.unit.id);
        graphRef.current?.focusNode(key);
        setSelected({
          unitId: target.unit.id,
          projectId: target.ref.project_id,
          treeVersion: target.ref.tree_version,
          nodeId: target.ref.node_id,
        });
        pendingFocus.current = null;
      }
    }
  }, [interactive, allVisibleUnits]);

  React.useEffect(() => {
    dispatchUniverseView({ mode: active ? "detail" : "overview", source_id: selected ? detail?.source_id ?? null : null, progress: active ? 1 : 0 });
  }, [active, selected, detail?.source_id]);

  const selectUnit = React.useCallback((unit: UniverseKnowledgeUnit, refOverride?: KnowledgeCluster) => {
    const targetRef = refOverride ?? active ?? (trees[0] ? {
      project_id: trees[0].project_id,
      tree_version: trees[0].tree_version,
      node_id: trees[0].nodes.find((n) => !n.parent_id)?.id ?? trees[0].nodes[0]?.id ?? "",
    } : null);
    if (!targetRef) return;
    setSelected({
      unitId: unit.id,
      projectId: targetRef.project_id,
      treeVersion: targetRef.tree_version,
      nodeId: targetRef.node_id,
    });
    graphRef.current?.focusNode(knowledgeUnitKey(targetRef, unit.id));
  }, [active, trees]);

  const focusResult = React.useCallback(() => {
    const target = matches[0];
    if (target) {
      navigate({ project_id: target.project_id, tree_version: target.tree_version, node_id: target.node_id }, target.unit_ids[0]);
    } else {
      const route = activation?.tree_routes?.[0];
      const nodeId = route?.node_ids[0];
      if (route && nodeId) navigate({ project_id: route.project_id, tree_version: route.tree_version, node_id: nodeId });
    }
  }, [matches, activation, navigate]);

  const back = React.useCallback(() => {
    if (selected) { clearSelection(); return; }
    navigate(current?.node.parent_id && active ? { ...active, node_id: current.node.parent_id } : null);
  }, [selected, clearSelection, navigate, current?.node.parent_id, active]);

  const status = manifest?.status ?? "ready";
  const empty = Boolean(manifest) && !loading && (trees.length === 0 || status !== "ready");
  const fallback = Boolean(sceneFailure);

  const visibleUnitList = React.useMemo(
    () => allVisibleUnits.map((item) => item.unit),
    [allVisibleUnits],
  );
  const unitRefMap = React.useMemo(
    () => new Map(allVisibleUnits.map((item) => [item.unit.id, item.ref])),
    [allVisibleUnits],
  );

  const docGroups = React.useMemo(
    () => groupUnitsByDocument(visibleUnitList, allVisibleEdges),
    [visibleUnitList, allVisibleEdges],
  );

  const activeTree = React.useMemo(() => {
    if (current?.tree) return current.tree;
    return trees[0] ?? null;
  }, [current, trees]);

  const jumpToSynapse = React.useCallback((synapse: UniverseKnowledgeSynapse) => {
    if (!selected) return;
    const found = allVisibleUnits.find(({ unit }) => unit.id === synapse.target_unit_id);
    if (found) {
      selectUnit(found.unit, found.ref);
      return;
    }
    if (synapse.target_node_id && synapse.target_node_id !== selected.nodeId) {
      navigate(
        {
          project_id: selected.projectId,
          tree_version: selected.treeVersion,
          node_id: synapse.target_node_id,
        },
        synapse.target_unit_id,
      );
      return;
    }
    setSelected({
      ...selected,
      unitId: synapse.target_unit_id,
    });
  }, [selected, allVisibleUnits, selectUnit, navigate]);

  const connectionReason = (synapse: UniverseKnowledgeSynapse) => {
    const entities = synapse.shared_entities?.filter((entity) => entity.length > 4 && !/^(?:DOC|ALL|GRP|ORG|NSC|STAFF)$/i.test(entity));
    return entities?.length ? t("atlas.sharedEntity", { entity: entities.slice(0, 2).join(", ") }) : t(synapse.cross_document ? "atlas.sharedContext" : "atlas.sameDocument");
  };

  const query = searchQuery.trim().toLocaleLowerCase();
  const matchesQuery = (text: string) => !query || text.toLocaleLowerCase().includes(query);

  React.useEffect(() => {
    if (showList) searchRef.current?.focus();
  }, [showList]);

  const renderUnitList = (fallbackList = false) => (
    <div id="sag-universe-browser" className={fallbackList ? styles.fallbackList : styles.browser} data-universe-fallback={fallbackList || undefined}>
      <div className={styles.browserHeading}>
        <span>{t("browse")}</span>
        {!fallbackList && <Button size="icon" variant="ghost" className="size-7" aria-label={t("close")} onClick={() => setShowList(false)}><X className="size-4" /></Button>}
      </div>
      <label className={styles.localSearch}>
        <Search aria-hidden="true" className="size-4 shrink-0" />
        <input ref={searchRef} value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder={t("atlas.findInView")} aria-label={t("atlas.findInView")} />
      </label>
      <div className={styles.browserBody}>
        {clusterChoices.filter((item) => (!active || knowledgeClusterKey(item.ref) !== activeKey) && matchesQuery(item.label)).map((item) => (
          <button key={knowledgeClusterKey(item.ref)} type="button" className={styles.unitRow} onClick={() => { setSearchQuery(""); navigate(item.ref); }}>
            <Orbit className="mt-0.5 size-4 shrink-0 text-[#c7bdb0]/80" />
            <span><strong>{item.label}</strong><small>{item.stats}</small></span>
            <ChevronRight className="mt-1 size-3.5 shrink-0" />
          </button>
        ))}
        {docGroups.map((group) => {
          const units = group.units.filter((unit) => matchesQuery(`${cleanNeuronLabel(unit)} ${unit.heading} ${unit.document_name}`));
          return units.length > 0 && <div key={group.document_id} className={styles.ganglionGroup}>
            <div className={styles.ganglionHeader}><div className={styles.ganglionTitle}><FileText className="size-3.5 shrink-0" /><strong>{humanizeDocName(group.document_name)}</strong></div></div>
            {units.map((unit) => <button key={unit.id} type="button" className={styles.unitRow} data-selected={selected?.unitId === unit.id} onClick={() => { selectUnit(unit, unitRefMap.get(unit.id)); setShowList(false); }}>
              <span className={styles.unitSomaDot} />
              <span><strong>{cleanNeuronLabel(unit)}</strong><small>{t("page", { page: unit.page_from })}</small></span>
              <ChevronRight className="mt-1 size-3.5 shrink-0" />
            </button>)}
          </div>;
        })}
        {pageLoading && <p className={styles.inlineStatus}><Loader2 className="size-4 animate-spin" />{t("loadingUnits")}</p>}
        {query && !pageLoading && !visibleUnitList.some((unit) => matchesQuery(`${cleanNeuronLabel(unit)} ${unit.heading} ${unit.document_name}`)) && !clusterChoices.some((item) => matchesQuery(item.label)) && <p className={styles.inlineStatus}>{t("atlas.noFilterResults")}</p>}
      </div>
    </div>
  );

  return (
    <div
      className={cn("sag-knowledge-universe absolute inset-0 z-[2] overflow-hidden", styles.universe, !interactive && "pointer-events-none opacity-0")}
      aria-label={t("title")}
      aria-hidden={!interactive}
      inert={!interactive}
      data-universe-mode={interactive ? "explore" : "normal"}
      data-universe-view={active ? "detail" : "overview"}
      data-universe-reduced-motion={Boolean(reducedMotion)}
      data-universe-knowledge="true"
      onKeyDown={(event) => {
        if (event.key === "Escape") { event.stopPropagation(); if (showList) setShowList(false); else back(); }
      }}
    >
      {manifest && !fallback && trees.length > 0 && (
        <div className={cn("sag-universe-graph", styles.graph)}>
          <NeuralAtlasScene
            key={sceneAttempt}
            ref={graphRef}
            regions={subtreeRegions}
            neurons={cortexNeurons}
            edges={cortexEdges}
            selectedKey={selectedSceneId}
            activeRegionKey={active ? activeKey : null}
            leftDrawerOpen={Boolean(showList && !selected)}
            rightDrawerOpen={Boolean(selected)}
            darkTheme={true}
            onUnavailable={setSceneFailure}
            keyboardHint={t("keyboardHint")}
            reducedMotion={Boolean(reducedMotion)}
            filterMode="all"
            onSelectRegion={(region) => {
              dispatchUniverseInteraction();
              const target = {
                project_id: region.project_id,
                tree_version: region.tree_version,
                node_id: region.node.id,
              };
              if (knowledgeClusterKey(target) !== activeKey) {
                navigate(target);
              } else {
                graphRef.current?.focusNode(region.key);
              }
            }}
            onSelectNeuron={(neuron) => {
              dispatchUniverseInteraction();
              const ref = unitRefMap.get(neuron.unit.id);
              selectUnit(neuron.unit, ref);
            }}
            onClearSelection={clearSelection}
          />
        </div>
      )}

      <header className={styles.topbar}>
        <button type="button" className={styles.wordmark} onClick={() => navigate(null)} aria-label="SAG">SAG</button>
        <span className={styles.workspaceName}>{activeTree?.project_id.replace(/^proj[-_]/, "").replace(/[-_]/g, " ") || "SAG"}<span>/</span>{t("atlas.knowledge")}</span>
        <div className={styles.topbarActions}>
          <button type="button" aria-expanded={showList} aria-controls="sag-universe-browser" onClick={() => { clearSelection(); setShowList((value) => !value); }}><Layers3 className="size-4" /><span>{t("browse")}</span></button>
          <button type="button" aria-label={t("atlas.exitViewer")} title={t("atlas.exitViewer")} onClick={exitExploreMode}><ArrowLeft className="size-4" /><span>{t("atlas.exitViewer")}</span></button>
        </div>
      </header>

      <header className={styles.header} data-universe-summary="true">
        <h1>{t("title")}</h1>
        <p>{t("subtitle")}</p>
        {sourceProjection && <p role="status" className={styles.sourceNotice}>{t("sourceNotice")}</p>}
      </header>
      <nav className={styles.breadcrumbs} aria-label={t("navigation")} data-universe-navigation="true">
        <button type="button" onClick={() => navigate(null)}>{t("overview")}</button>
        {trail.map((node, index) => <React.Fragment key={node.id}>
          <span>/</span><button type="button" aria-current={index === trail.length - 1 ? "location" : undefined} onClick={() => active && navigate({ ...active, node_id: node.id })}>
            {formatCorticalRegionLabel(node, unitsByNodeSubtree.get(node.id) ?? [], t("clusterFallback", { index: index + 1 }))}
          </button>
        </React.Fragment>)}
        {!active && detail && selected && <><span>/</span><button type="button" className={styles.breadcrumbSelection} onClick={() => navigate({ project_id: selected.projectId, tree_version: selected.treeVersion, node_id: selected.nodeId })}>{subtreeRegions.find((region) => region.node.id === selected.nodeId)?.label ?? humanizeDocName(detail.document_name)}</button></>}
      </nav>

      {loading && <div className={styles.centerStatus} role="status"><div className={styles.loadingOrbit}><Brain className="size-7 text-[#d49b62]" /></div><h2>{t("loading")}</h2><p>{t("loadingHint")}</p></div>}
      {empty && <div className={styles.centerStatus}><Layers3 className="mb-4 size-8 text-[#d49b62]/70" /><h2>{t(status === "failed" ? "failedTitle" : status === "stale" ? "staleTitle" : "emptyTitle")}</h2><p>{t(status === "failed" ? "failedHint" : status === "stale" ? "staleHint" : "emptyHint")}</p><Button variant="outline" className="mt-5" onClick={refresh}><RefreshCw className="mr-2 size-3.5" />{t("refresh")}</Button></div>}
      {error && <div role="alert" className={styles.error}><span>{error}</span><Button variant="ghost" size="sm" onClick={refresh}>{t("retry")}</Button></div>}
      {pageLoading && !loading && <div className={styles.loadingPill} role="status"><Loader2 className="size-3.5 animate-spin" />{t("loadingUnits")}</div>}

      {fallback && trees.length > 0 && <>
        <div className={styles.fallbackNotice} role="status"><p>{webglText("title")} · {t("listAvailable")}</p><Button size="sm" variant="outline" onClick={() => { setSceneFailure(null); setSceneAttempt((value) => value + 1); }}>{webglText("retry")}</Button></div>
        {renderUnitList(true)}
      </>}
      {!fallback && showList && !selected && renderUnitList()}
      {selected && <aside className={styles.detail} aria-label={t("details")} data-universe-detail-panel="true" data-selected-unit={detail?.id}>
        <div className={styles.browserHeading}>
          <span>{t("atlas.selectedKnowledge")}</span>
          <button type="button" aria-label={t("close")} onClick={clearSelection}><X className="size-4" /></button>
        </div>
        {detailLoading && <p className={styles.inlineStatus} role="status"><Loader2 className="size-4 animate-spin" />{t("loadingEvidence")}</p>}
        {detail && <div className={styles.detailContent}>
          <h2 aria-live="polite">{cleanNeuronLabel(detail)}</h2>
          <p className={styles.sourceMeta}>{humanizeDocName(detail.document_name)} · {t("page", { page: detail.page_from })}</p>
          <div className={styles.passage}><MarkdownContent content={knowledgePassage(detail.content).text || detail.preview} /></div>
          {!sourceProjection && Boolean(detail.synapses?.length) && <section className={styles.connections}>
            <h3>{t("related")}</h3>
            {detail.synapses?.map((synapse) => <button type="button" key={synapse.target_unit_id} className={styles.connectionRow} onClick={() => jumpToSynapse(synapse)}>
              <span><strong>{knowledgeUnitTitle({ label: synapse.target_label, heading: synapse.target_heading, preview: synapse.target_preview, document_name: synapse.target_document_name })}</strong><small>{connectionReason(synapse)}</small></span>
              <ChevronRight className="size-4 shrink-0" />
            </button>)}
          </section>}
        </div>}
      </aside>}

      {activation && <div className={styles.result}>
        <span className={styles.beacon} /><span title={activation.query}>{activation.query}</span>
        <Button size="sm" variant="ghost" disabled={resolving || (!matches.length && !routedNodes.size)} onClick={focusResult}>
          {resolving ? <Loader2 className="mr-1 size-3 animate-spin" /> : <Focus className="mr-1 size-3" />}{t("focusResult")}
        </Button>
        <button type="button" aria-label={t("clearResults")} onClick={() => { setActivation(null); setMatches([]); }}><X className="size-3.5" /></button>
      </div>}

      {manifest && trees.length > 0 && <>
        <div className={styles.tools}>
          {active && <Button variant="outline" size="icon" title={t("back")} aria-label={t("back")} onClick={back}><ArrowLeft className="size-4" /></Button>}
          <Button variant="outline" size="icon" title={t("fit")} aria-label={t("fit")} disabled={fallback} onClick={() => graphRef.current?.focusAll()}><Focus className="size-5" /></Button>
          <Button variant="outline" size="icon" title={t("atlas.zoomOut")} aria-label={t("atlas.zoomOut")} disabled={fallback} onClick={() => graphRef.current?.zoomOut()}><Minus className="size-5" /></Button>
          <Button variant="outline" size="icon" title={t("atlas.zoomIn")} aria-label={t("atlas.zoomIn")} disabled={fallback} onClick={() => graphRef.current?.zoomIn()}><Plus className="size-5" /></Button>
        </div>
        <footer className={styles.legend}>
          <span><i data-kind="knowledge" />{t("unit")}</span><span><i data-kind="evidence" />{t("atlas.relatedEvidence")}</span><small>{t("gestureHint")}</small>
        </footer>
      </>}

      {!selected && current?.node.is_leaf && page && <div className={styles.pagination} data-universe-pagination="true">
        <button type="button" aria-label={t("previous")} disabled={pageIndex === 0 || pageLoading} onClick={() => { clearSelection(); setPageIndex((value) => value - 1); }}><ChevronLeft className="size-4" /></button>
        <span>{t("range", { from: page.total ? pageIndex * pageSize + 1 : 0, to: Math.min((pageIndex + 1) * pageSize, page.total), total: page.total })}</span>
        <button type="button" aria-label={t("next")} disabled={!page.next_cursor || pageLoading} onClick={() => { if (page.next_cursor) { clearSelection(); setCursors((value) => [...value.slice(0, pageIndex + 1), page.next_cursor!]); setPageIndex((value) => value + 1); } }}><ChevronRight className="size-4" /></button>
      </div>}
    </div>
  );
}
