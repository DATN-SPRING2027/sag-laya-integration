"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { useReducedMotion } from "motion/react";
import {
  ArrowLeft, ArrowUpRight, BookOpen, ChevronLeft, ChevronRight, Focus,
  Layers3, List, Loader2, Orbit, RefreshCw, Sparkles, X,
} from "lucide-react";

import { api, ApiError } from "@/lib/api";
import type {
  UniverseActivation, UniverseKnowledgeDetail, UniverseKnowledgeMatch,
  UniverseKnowledgePage, UniverseKnowledgeUnit, UniverseManifest,
} from "@/lib/types";
import {
  dispatchUniverseAsk, dispatchUniverseInteraction, dispatchUniverseView,
  UNIVERSE_ACTIVATE_EVENT, UNIVERSE_FOCUS_EVENT, UNIVERSE_RESET_EVENT,
  UNIVERSE_SOURCE_FOCUS_EVENT,
} from "@/lib/universe-events";
import {
  findKnowledgeCluster, knowledgeClusterKey, knowledgeClusterTrail,
  knowledgeUnitKey, projectKnowledgeUniverse, type KnowledgeCluster,
} from "@/lib/universe-knowledge-projection";
import { detectUniverseWebGLCapability } from "@/lib/universe-webgl-capability";
import { useUniverseViewPreferences } from "@/lib/universe-view-preferences";
import { cn } from "@/lib/utils";
import { useApp } from "@/components/features/app-shell";
import { useDetailPanel } from "@/components/features/detail-panel";
import { UniverseScene } from "@/components/features/universe-scene";
import type {
  UniverseSceneHandle, UniverseSceneNode, UniverseSceneUnavailableReason,
} from "@/components/features/universe-scene-contract";
import { Button } from "@/components/ui/button";
import styles from "./knowledge-routing-universe.module.css";

const NOOP = () => {};
const NO_TIMELINE = {
  enabled: false, phase: "idle" as const, hasNext: false, hasPrevious: false,
  networkExhausted: true, revision: 0,
};
const ROOT_PAGE_SIZE = 8;
const EMPTY_TREES: UniverseManifest["trees"] = [];
const NO_SOURCE_HITS: [] = [];

export function KnowledgeRoutingUniverse({ interactive = true }: { interactive?: boolean }) {
  const t = useTranslations("KnowledgeUniverse.knowledge");
  const webglText = useTranslations("KnowledgeUniverse.webgl");
  const { resolvedTheme } = useTheme();
  const reducedMotion = useReducedMotion();
  const { preferences } = useUniverseViewPreferences();
  const detailPanel = useDetailPanel();
  const { exitExploreMode } = useApp();
  const graphRef = React.useRef<UniverseSceneHandle>(null);
  const [manifest, setManifest] = React.useState<UniverseManifest | null>(null);
  const [revision, setRevision] = React.useState(0);
  const [sceneEpoch, setSceneEpoch] = React.useState(0);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState("");
  const [active, setActive] = React.useState<KnowledgeCluster | null>(null);
  const [page, setPage] = React.useState<UniverseKnowledgePage | null>(null);
  const [pageLoading, setPageLoading] = React.useState(false);
  const [cursors, setCursors] = React.useState<Array<string | undefined>>([undefined]);
  const [pageIndex, setPageIndex] = React.useState(0);
  const [rootPage, setRootPage] = React.useState(0);
  const [relatedTo, setRelatedTo] = React.useState<UniverseKnowledgeUnit | null>(null);
  const [selected, setSelected] = React.useState<string | null>(null);
  const [detail, setDetail] = React.useState<UniverseKnowledgeDetail | null>(null);
  const [detailLoading, setDetailLoading] = React.useState(false);
  const [showList, setShowList] = React.useState(false);
  const [activation, setActivation] = React.useState<UniverseActivation | null>(null);
  const [matches, setMatches] = React.useState<UniverseKnowledgeMatch[]>([]);
  const [resolving, setResolving] = React.useState(false);
  const [sceneFailure, setSceneFailure] = React.useState<UniverseSceneUnavailableReason | "api-unavailable" | null>(null);
  const [sceneAttempt, setSceneAttempt] = React.useState(0);
  const views = React.useRef(new Map<string, ReturnType<UniverseSceneHandle["captureExplorationView"]>>());
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
    graphRef.current?.unlockNode();
  }, []);

  const navigate = React.useCallback((target: KnowledgeCluster | null, focusUnit?: string) => {
    if (target && !findKnowledgeCluster(trees, target)) {
      setError(t("snapshotChanged"));
      return;
    }
    pendingFocus.current = focusUnit ?? null;
    const view = graphRef.current?.captureExplorationView();
    if (view) views.current.set(activeKey, view);
    if (views.current.has(target ? knowledgeClusterKey(target) : "overview")) {
      graphRef.current?.prepareExplorationRestore();
    }
    graphRef.current?.unlockNode();
    setSceneEpoch((value) => value + 1);
    setActive(target ? { project_id: target.project_id, tree_version: target.tree_version, node_id: target.node_id } : null);
    setPage(null);
    setPageLoading(false);
    setSelected(null);
    setDetail(null);
    setDetailLoading(false);
    setRelatedTo(null);
    setCursors([undefined]);
    setPageIndex(0);
    setRootPage(0);
    setError("");
  }, [activeKey, trees, t]);

  const onFailure = React.useCallback((reason: unknown) => {
    if (reason instanceof ApiError && reason.code === "aborted") return;
    if (reason instanceof DOMException && reason.name === "AbortError") return;
    setError(reason instanceof ApiError ? reason.message : t("loadFailed"));
    if (reason instanceof ApiError && [401, 403, 404, 409].includes(reason.status)) {
      setPage(null);
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
      setDetail(null);
      setSelected(null);
      setRelatedTo(null);
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
      // A small corpus opens at its real leaf; membership and clustering stay unchanged.
      if (roots.length === 1 && roots[0].node.is_leaf) {
        const { tree, node } = roots[0];
        setActive({ project_id: tree.project_id, tree_version: tree.tree_version, node_id: node.id });
      } else {
        setActive(null);
      }
      setCursors([undefined]);
      setPageIndex(0);
      setRootPage(0);
    }).catch((reason) => { if (!controller.signal.aborted) onFailure(reason); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [interactive, revision, onFailure]);

  const refresh = React.useCallback(() => {
    clearSelection();
    setPage(null);
    setManifest(null);
    setActive(null);
    setRelatedTo(null);
    setMatches([]);
    views.current.clear();
    setSceneEpoch((value) => value + 1);
    setRevision((value) => value + 1);
  }, [clearSelection]);

  React.useEffect(() => {
    if (!interactive || !active || !current?.node.is_leaf) return;
    const controller = new AbortController();
    setPageLoading(true);
    setPage(null);
    setError("");
    void api.universeKnowledgeCluster({
      ...active, limit: pageSize, cursor: cursors[pageIndex], related_to: relatedTo?.id,
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
  }, [interactive, active, current?.node.is_leaf, pageSize, cursors, pageIndex, relatedTo?.id, onFailure, t]);

  React.useEffect(() => {
    setCursors([undefined]);
    setPageIndex(0);
  }, [pageSize]);

  React.useEffect(() => {
    if (!interactive || !active || !selected || !page) return;
    const controller = new AbortController();
    setDetailLoading(true);
    setDetail(null);
    void api.universeKnowledgeUnit(selected, active.project_id, active.tree_version, controller.signal).then((value) => {
      if (controller.signal.aborted) return;
      if (value.scope_key !== page.scope_key) {
        onFailure(new ApiError(409, "snapshot_changed", t("snapshotChanged")));
        return;
      }
      setDetail(value);
    }).catch((reason) => { if (!controller.signal.aborted) onFailure(reason); })
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [interactive, active, selected, page, onFailure, t]);

  React.useEffect(() => {
    const controller = new AbortController();
    const onActivation = (event: Event) => {
      const value = (event as CustomEvent<UniverseActivation>).detail;
      if (value) { setActivation(value); setMatches([]); }
    };
    const onReset = () => { setActivation(null); setMatches([]); navigate(null); };
    const onFocus = (event: Event) => {
      const value = (event as CustomEvent<{ id?: string; source_id?: string }>).detail;
      const unit = page?.units.find((item) => item.block_id === value?.id || item.id === value?.id);
      if (unit && active) { setSelected(unit.id); graphRef.current?.focusNode(knowledgeUnitKey(active, unit.id)); }
    };
    const onSourceFocus = (event: Event) => {
      const value = (event as CustomEvent<{ source_id?: string }>).detail;
      const unit = page?.units.find((item) => item.source_id === value?.source_id);
      if (unit && active) {
        graphRef.current?.focusNode(knowledgeUnitKey(active, unit.id));
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
  }, [navigate, page, active, onFailure]);

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

  const clusterChoices = React.useMemo(() => {
    const choices = current && active
      ? [{ tree: current.tree, node: current.node }, ...current.tree.nodes
        .filter((node) => node.parent_id === active.node_id).map((node) => ({ tree: current.tree, node }))]
      : trees.flatMap((tree) => tree.nodes.filter((node) => !node.parent_id).map((node) => ({ tree, node })));
    return choices.map(({ tree, node }, index) => ({
      ref: { project_id: tree.project_id, tree_version: tree.tree_version, node_id: node.id },
      node, label: node.label || t("clusterFallback", { index: index + 1 }),
      stats: t("clusterStats", { units: node.unit_count, sources: node.source_count }),
    }));
  }, [current, active, trees, t]);
  const visibleClusters = React.useMemo(
    () => clusterChoices.slice(rootPage * ROOT_PAGE_SIZE, (rootPage + 1) * ROOT_PAGE_SIZE),
    [clusterChoices, rootPage],
  );
  const matchedClusters = React.useMemo(() => new Set(matches.flatMap((match) =>
    knowledgeClusterTrail(trees, match).map((node) => knowledgeClusterKey({ ...match, node_id: node.id })),
  )), [matches, trees]);
  const units = React.useMemo(() => {
    const list = page?.units ?? [];
    return relatedTo && page && !list.some((unit) => unit.id === relatedTo.id) ? [relatedTo, ...list] : list;
  }, [page, relatedTo]);
  const data = React.useMemo(() => projectKnowledgeUniverse({
    clusters: visibleClusters, active: current?.node.is_leaf ? active : null,
    units, edges: page?.edges ?? [], matches, routedNodes, matchedClusters,
    unitKind: t("unit"), clusterKind: t(sourceProjection ? "source" : "cluster"),
    relatedHint: t(sourceProjection ? "sourceRelatedHint" : "relatedHint"), relationsAvailable: !sourceProjection, epoch: sceneEpoch,
  }), [visibleClusters, active, current?.node.is_leaf, units, page?.edges, matches, routedNodes, matchedClusters, sourceProjection, t, sceneEpoch]);
  const trail = knowledgeClusterTrail(trees, active);
  const title = current ? clusterChoices[0]?.label : t("title");
  const selectedSceneId = active && selected ? knowledgeUnitKey(active, selected) : null;

  React.useEffect(() => {
    if (!interactive) return;
    const frame = window.requestAnimationFrame(() => {
      const view = views.current.get(activeKey);
      if (view && !pendingFocus.current) graphRef.current?.restoreExplorationView(view);
      if (pendingFocus.current && active && units.some((unit) => unit.id === pendingFocus.current)) {
        graphRef.current?.focusNode(knowledgeUnitKey(active, pendingFocus.current));
        setSelected(pendingFocus.current);
        pendingFocus.current = null;
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [interactive, activeKey, active, units, sceneAttempt]);

  React.useEffect(() => {
    if (pendingFocus.current && page && !units.some((unit) => unit.id === pendingFocus.current) && page.next_cursor) {
      setCursors((value) => [...value.slice(0, pageIndex + 1), page.next_cursor!]);
      setPageIndex((value) => value + 1);
    }
  }, [page, pageIndex, units]);

  React.useEffect(() => {
    dispatchUniverseView({ mode: active ? "detail" : "overview", source_id: selected ? detail?.source_id ?? null : null, progress: active ? 1 : 0 });
  }, [active, selected, detail?.source_id]);

  const selectUnit = React.useCallback((unit: UniverseKnowledgeUnit) => {
    setSelected(unit.id);
    if (active) graphRef.current?.lockNode(knowledgeUnitKey(active, unit.id));
  }, [active]);
  const selectNode = React.useCallback((node: UniverseSceneNode) => {
    dispatchUniverseInteraction();
    if (node.kind === "source") {
      const target = clusterChoices.find((item) => knowledgeClusterKey(item.ref) === node.id);
      if (target && knowledgeClusterKey(target.ref) !== activeKey) navigate(target.ref);
      else if (active) graphRef.current?.focusSource(knowledgeClusterKey(active));
    } else {
      const unit = units.find((item) => item.id === node.rawId);
      if (unit) selectUnit(unit);
    }
  }, [clusterChoices, activeKey, active, navigate, units, selectUnit]);
  const exploreRelated = React.useCallback((unit: UniverseKnowledgeUnit) => {
    clearSelection();
    setRelatedTo(unit);
    setCursors([undefined]);
    setPageIndex(0);
  }, [clearSelection]);
  const askUnit = React.useCallback((unit: UniverseKnowledgeUnit) => {
    dispatchUniverseAsk({
      kind: "unit", rawId: unit.id, sourceId: unit.source_id, label: unit.label,
      prompt: t("askPrompt", { title: unit.label, document: unit.document_name, page: unit.page_from, text: unit.preview }),
    });
  }, [t]);
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
    if (relatedTo) { setRelatedTo(null); setCursors([undefined]); setPageIndex(0); return; }
    navigate(current?.node.parent_id && active ? { ...active, node_id: current.node.parent_id } : null);
  }, [selected, relatedTo, clearSelection, navigate, current?.node.parent_id, active]);

  const fallback = sceneFailure !== null;
  const empty = !loading && !error && trees.length === 0;
  const status = manifest?.status;
  const renderUnitList = (fallbackList = false) => (
    <div className={cn(styles.browser, fallbackList && styles.fallbackBrowser)} aria-label={t("browse")} data-universe-browser="true">
      <div className={styles.browserHeading}>
        <span>{relatedTo ? t("related") : t("browse")}</span>
        {!fallbackList && <Button variant="ghost" size="icon" className="size-7" aria-label={t("close")} onClick={() => setShowList(false)}><X className="size-3.5" /></Button>}
      </div>
      <div className={styles.browserItems}>
        {current?.node.is_leaf ? (page?.units ?? []).map((unit, index) => (
          <button key={unit.id} type="button" className={styles.unitRow} data-selected={selected === unit.id} onClick={() => selectUnit(unit)}>
            <span className={styles.unitNumber}>{String(pageIndex * pageSize + index + 1).padStart(2, "0")}</span>
            <span><strong>{unit.label}</strong><small>{unit.document_name} · {t("page", { page: unit.page_from })}</small></span>
          </button>
        )) : visibleClusters.filter((item) => !active || knowledgeClusterKey(item.ref) !== activeKey).map((item) => (
          <button key={knowledgeClusterKey(item.ref)} type="button" className={styles.unitRow} onClick={() => navigate(item.ref)}>
            <Orbit className="size-4 shrink-0 text-amber-400/80" /><span><strong>{item.label}</strong><small>{item.stats}</small></span><ChevronRight className="size-3.5 shrink-0" />
          </button>
        ))}
        {pageLoading && <p className={styles.inlineStatus}><Loader2 className="size-4 animate-spin" />{t("loadingUnits")}</p>}
        {page && page.units.length === 0 && !pageLoading && <p className={styles.inlineStatus}>{t(relatedTo ? "noRelated" : "noUnits")}</p>}
      </div>
    </div>
  );

  return (
    <div className={cn("sag-knowledge-universe absolute inset-0 z-[2] overflow-hidden", styles.universe, !interactive && "pointer-events-none opacity-0")}
      aria-label={t("title")} aria-hidden={!interactive} inert={!interactive}
      data-universe-mode={interactive ? "explore" : "normal"} data-universe-view={active ? "detail" : "overview"}
      data-universe-reduced-motion={Boolean(reducedMotion)} data-universe-knowledge="true"
      onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); back(); } }}>
      {manifest && !fallback && trees.length > 0 && (
        <div className="sag-universe-graph absolute inset-0">
          <UniverseScene key={sceneAttempt} ref={graphRef} data={data} strategy="knowledge" policy={manifest.policy}
            sourceHits={NO_SOURCE_HITS} selectedId={selectedSceneId} darkTheme={resolvedTheme !== "light"}
            interactive={interactive} reducedMotion={Boolean(reducedMotion)} viewPreferences={preferences}
            timelineJourney={NO_TIMELINE} onNodeClick={selectNode} onHover={NOOP} onViewChange={NOOP}
            onSourceLod={NOOP} onSourceWheel={(id) => {
              const target = clusterChoices.find((item) => knowledgeClusterKey(item.ref) === id);
              if (target && knowledgeClusterKey(target.ref) !== activeKey) navigate(target.ref);
            }}
            onSelectionClear={clearSelection} onBackRequest={back} onTimelineIntent={() => "complete"} onTimelineSettled={NOOP}
            onViewDetails={selectNode} onExploreMore={(node) => { const unit = units.find((item) => item.id === node.rawId); if (unit) exploreRelated(unit); }}
            onAskNode={(node) => { const unit = units.find((item) => item.id === node.rawId); if (unit) askUnit(unit); }}
            actionLabels={{ viewDetails: t("details"), exploreMore: t("related"), askAi: t("ask") }}
            onUnavailable={setSceneFailure} onUserInteraction={dispatchUniverseInteraction} />
        </div>
      )}

      <header className={styles.header} data-universe-summary="true">
        <div className={styles.eyebrow}><span className={styles.beacon} />SAG · {t("eyebrow")}</div>
        <h1>{title}</h1>
        <p>{manifest ? t(sourceProjection ? "sourceSummary" : "summary", { projects: trees.length, sources: manifest.counts.sources ?? 0, clusters: manifest.counts.nodes ?? 0, units: manifest.counts.units ?? 0 }) : t("subtitle")}</p>
        {sourceProjection && <p role="status" className={styles.sourceNotice}>{t("sourceNotice")}</p>}
      </header>

      <nav className={styles.breadcrumbs} aria-label={t("navigation")} data-universe-navigation="true">
        <button type="button" onClick={() => navigate(null)} aria-label={t("overview")}><Orbit className="size-3.5" /><span>{t("overview")}</span></button>
        {trail.map((node, index) => <React.Fragment key={node.id}>
          <ChevronRight className="size-3 text-muted-foreground/60" />
          <button type="button" aria-current={index === trail.length - 1 ? "location" : undefined}
            onClick={() => active && navigate({ ...active, node_id: node.id })}>
            {node.label || t("clusterFallback", { index: index + 1 })}
          </button>
        </React.Fragment>)}
      </nav>

      {loading && <div className={styles.centerStatus} role="status"><div className={styles.loadingOrbit}><Orbit className="size-8" /></div><h2>{t("loading")}</h2><p>{t("loadingHint")}</p></div>}
      {empty && <div className={styles.centerStatus}><Layers3 className="mb-4 size-8 text-amber-400/70" /><h2>{t(status === "failed" ? "failedTitle" : status === "stale" ? "staleTitle" : "emptyTitle")}</h2><p>{t(status === "failed" ? "failedHint" : status === "stale" ? "staleHint" : "emptyHint")}</p><Button variant="outline" className="mt-5" onClick={refresh}><RefreshCw className="mr-2 size-3.5" />{t("refresh")}</Button></div>}
      {error && <div role="alert" className={styles.error}><span>{error}</span><Button variant="ghost" size="sm" onClick={refresh}>{t("retry")}</Button></div>}
      {pageLoading && !loading && <div className={styles.loadingPill} role="status"><Loader2 className="size-3.5 animate-spin" />{t("loadingUnits")}</div>}

      {fallback && trees.length > 0 && <>
        <div className={styles.fallbackNotice} role="status"><p>{webglText("title")} · {t("listAvailable")}</p><Button size="sm" variant="outline" onClick={() => { setSceneFailure(null); setSceneAttempt((value) => value + 1); }}>{webglText("retry")}</Button></div>
        {renderUnitList(true)}
      </>}
      {!fallback && showList && !selected && renderUnitList()}

      {selected && <aside className={styles.detail} aria-label={t("details")} data-universe-detail-panel="true">
        <div className={styles.browserHeading}><span><Sparkles className="size-3.5" />{t("unit")}</span><Button size="icon" variant="ghost" className="size-7" aria-label={t("close")} onClick={clearSelection}><X className="size-4" /></Button></div>
        {detailLoading && <p className={styles.inlineStatus} role="status"><Loader2 className="size-4 animate-spin" />{t("loadingEvidence")}</p>}
        {detail && <div className={styles.detailContent}>
          <h2>{detail.label}</h2>
          <div className={styles.provenance}><BookOpen className="size-4 shrink-0" /><span>{detail.document_name}<small>{detail.source_name} · {t("version", { version: detail.version_no })} · {t("page", { page: detail.page_from })}</small></span></div>
          <p className={styles.passage}>{detail.content}</p>
          <div className={styles.detailActions}>
            <Button size="sm" variant="outline" onClick={() => { detailPanel.open({ kind: "document", sourceId: detail.source_id, documentId: detail.document_id, title: detail.document_name }); exitExploreMode(); }}><ArrowUpRight className="mr-1.5 size-3.5" />{t("openEvidence")}</Button>
            <Button size="sm" variant="outline" onClick={() => askUnit(detail)}><Sparkles className="mr-1.5 size-3.5" />{t("ask")}</Button>
          </div>
          <div className={styles.evidenceHeading}>{t("evidenceCount", { count: detail.evidence.length })}</div>
          {detail.evidence.map((evidence) => <blockquote key={evidence.id} className={styles.evidence}>
            <div><span>{evidence.tier} · {evidence.kind}</span><span>{t(evidence.candidate ? "candidate" : "recorded")}</span></div>
            <p>{evidence.quote}</p>
          </blockquote>)}
          {detail.evidence.length === 0 && <p className="text-xs text-muted-foreground">{t("noExtractedEvidence")}</p>}
          {sourceProjection ? <p className="mt-4 text-xs text-muted-foreground">{t("sourceRelatedHint")}</p>
            : <Button variant="ghost" size="sm" className="mt-3 w-full" onClick={() => exploreRelated(detail)}><Layers3 className="mr-1.5 size-3.5" />{t("related")}</Button>}
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
          <Button variant="outline" size="icon" title={t("browse")} aria-label={t("browse")} aria-pressed={showList} onClick={() => { clearSelection(); setShowList((value) => !value); }}><List className="size-4" /></Button>
          <Button variant="outline" size="icon" title={t("fit")} aria-label={t("fit")} disabled={fallback} onClick={() => { clearSelection(); if (active && current?.node.is_leaf) graphRef.current?.focusSource(knowledgeClusterKey(active)); else graphRef.current?.focusOverview(); }}><Focus className="size-4" /></Button>
          <Button variant="outline" size="icon" title={t("refresh")} aria-label={t("refresh")} onClick={refresh}><RefreshCw className="size-4" /></Button>
        </div>
        <footer className={styles.legend}><span><i />{t(sourceProjection ? "source" : "cluster")}</span><span><i className={styles.starMark} />{t("unit")}</span>{!sourceProjection && <span className={styles.linkLegend}>— {t("similarity")}</span>}<small>{t("gestureHint")}</small></footer>
      </>}

      {!selected && page && <div className={styles.pagination} data-universe-pagination="true">
        {relatedTo && <button type="button" onClick={() => { setRelatedTo(null); setCursors([undefined]); setPageIndex(0); }} title={t("back")} aria-label={t("back")}><ArrowLeft className="size-3.5" /></button>}
        <button type="button" aria-label={t("previous")} disabled={pageIndex === 0 || pageLoading} onClick={() => { clearSelection(); setPageIndex((value) => value - 1); }}><ChevronLeft className="size-4" /></button>
        <span>{t("range", { from: page.total ? pageIndex * pageSize + 1 : 0, to: Math.min((pageIndex + 1) * pageSize, page.total), total: page.total })}</span>
        <button type="button" aria-label={t("next")} disabled={!page.next_cursor || pageLoading} onClick={() => { if (page.next_cursor) { clearSelection(); setCursors((value) => [...value.slice(0, pageIndex + 1), page.next_cursor!]); setPageIndex((value) => value + 1); } }}><ChevronRight className="size-4" /></button>
      </div>}
      {clusterChoices.length > ROOT_PAGE_SIZE && <div className={styles.pagination} data-universe-pagination="true">
        <button type="button" aria-label={t("previous")} disabled={rootPage === 0} onClick={() => setRootPage((value) => value - 1)}><ChevronLeft className="size-4" /></button>
        <span>{rootPage + 1} / {Math.ceil(clusterChoices.length / ROOT_PAGE_SIZE)}</span>
        <button type="button" aria-label={t("next")} disabled={(rootPage + 1) * ROOT_PAGE_SIZE >= clusterChoices.length} onClick={() => setRootPage((value) => value + 1)}><ChevronRight className="size-4" /></button>
      </div>}
    </div>
  );
}
