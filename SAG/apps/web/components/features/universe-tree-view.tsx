"use client";

import * as React from "react";

import type { UniverseTree, UniverseTreeRoute } from "@/lib/types";

type UniverseTreeViewProps = {
  trees: UniverseTree[];
  routes: UniverseTreeRoute[];
  labels: {
    empty: string;
    project: string;
    nodes: string;
    units: string;
    sources: string;
    routed: string;
    branches: string;
    expand: string;
    collapse: string;
  };
};

type PositionedNode = {
  id: string;
  x: number;
  y: number;
  hasChildren: boolean;
};

function nodeKey(projectId: string, treeVersion: string, nodeId: string) {
  return `${projectId}\u0000${treeVersion}\u0000${nodeId}`;
}

function routedNodeIds(tree: UniverseTree, routes: UniverseTreeRoute[]) {
  const byId = new Map(tree.nodes.map((node) => [node.id, node]));
  const selected = new Set<string>();
  const path = new Set<string>();
  routes
    .filter((route) => route.project_id === tree.project_id && route.tree_version === tree.tree_version)
    .forEach((route) => route.node_ids.forEach((id) => {
      let current = byId.get(id);
      if (!current) return;
      selected.add(id);
      while (current && !path.has(current.id)) {
        path.add(current.id);
        current = current.parent_id ? byId.get(current.parent_id) : undefined;
      }
    }));
  return { path, selected };
}

export function UniverseTreeView({ trees, routes, labels }: UniverseTreeViewProps) {
  const routedByTree = React.useMemo(
    () => new Map(trees.map((tree) => [
      nodeKey(tree.project_id, tree.tree_version, ""),
      routedNodeIds(tree, routes),
    ])),
    [routes, trees],
  );
  const [expanded, setExpanded] = React.useState<Set<string>>(() => new Set());
  const [selectedKey, setSelectedKey] = React.useState<string | null>(null);

  React.useEffect(() => {
    setExpanded((current) => {
      const validKeys = new Set(
        trees.flatMap((tree) => tree.nodes.map((node) =>
          nodeKey(tree.project_id, tree.tree_version, node.id),
        )),
      );
      const next = new Set([...current].filter((key) => validKeys.has(key)));
      for (const tree of trees) {
        const childCounts = new Map<string, number>();
        tree.nodes.forEach((node) => {
          if (node.parent_id) childCounts.set(node.parent_id, (childCounts.get(node.parent_id) ?? 0) + 1);
        });
        tree.nodes.filter((node) => !node.parent_id).forEach((root) => {
          // shortcut: auto-expands roots with at most 32 children; add child paging if wider roots must open at once.
          if ((childCounts.get(root.id) ?? 0) <= 32) {
            next.add(nodeKey(tree.project_id, tree.tree_version, root.id));
          }
        });
        const routed = routedByTree.get(nodeKey(tree.project_id, tree.tree_version, ""));
        routed?.path.forEach((id) => next.add(nodeKey(tree.project_id, tree.tree_version, id)));
      }
      return next;
    });
  }, [routedByTree, trees]);

  const layouts = React.useMemo(() => trees.map((tree) => {
    const byId = new Map(tree.nodes.map((node) => [node.id, node]));
    const children = new Map<string, string[]>();
    for (const node of tree.nodes) {
      if (!node.parent_id) continue;
      const siblings = children.get(node.parent_id) ?? [];
      siblings.push(node.id);
      children.set(node.parent_id, siblings);
    }
    for (const siblings of children.values()) {
      siblings.sort((left, right) =>
        (byId.get(left)?.label ?? left).localeCompare(byId.get(right)?.label ?? right),
      );
    }

    let leafIndex = 0;
    const positions = new Map<string, PositionedNode>();
    const roots = tree.nodes.filter((node) => !node.parent_id).map((node) => node.id);
    const place = (id: string): number => {
      const node = byId.get(id);
      if (!node) return 0;
      const key = nodeKey(tree.project_id, tree.tree_version, id);
      positions.set(id, {
        id,
        x: 0,
        y: 76 + node.depth * 148,
        hasChildren: (children.get(id)?.length ?? 0) > 0,
      });
      const visibleChildren = expanded.has(key) ? children.get(id) ?? [] : [];
      const childXs = visibleChildren.map(place);
      const x = childXs.length
        ? childXs.reduce((sum, value) => sum + value, 0) / childXs.length
        : 96 + leafIndex++ * 220;
      positions.set(id, { ...positions.get(id)!, x });
      return x;
    };
    roots.forEach(place);
    const maxDepth = Math.max(0, ...[...positions.keys()].map((id) => byId.get(id)?.depth ?? 0));
    return {
      tree,
      byId,
      children,
      positions,
      width: Math.max(880, 96 + Math.max(1, leafIndex) * 220),
      height: 150 + (maxDepth + 1) * 148,
      routed: routedByTree.get(nodeKey(tree.project_id, tree.tree_version, ""))
        ?? { path: new Set<string>(), selected: new Set<string>() },
    };
  }), [expanded, routedByTree, trees]);

  const toggleNode = React.useCallback((key: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  if (trees.length === 0) {
    return (
      <div className="absolute inset-0 grid place-items-center p-8">
        <p className="max-w-md rounded-lg border border-border/60 bg-background/70 px-5 py-4 text-center text-sm text-muted-foreground shadow-soft backdrop-blur-md">
          {labels.empty}
        </p>
      </div>
    );
  }

  return (
    <div className="absolute inset-0 overflow-auto bg-background/20 pb-12 pt-20 backdrop-blur-[1px]">
      <div className="mx-auto flex w-max min-w-full flex-col gap-8 px-5 sm:px-8">
        {layouts.map(({ tree, byId, children, positions, width, height, routed }) => (
          <section
            key={`${tree.project_id}:${tree.tree_version}`}
            className="w-max min-w-full rounded-lg border border-border/40 bg-background/38 shadow-soft"
            aria-label={`${labels.project} ${tree.project_id}`}
          >
            <div className="sticky left-0 z-[1] flex w-fit max-w-[calc(100vw-2.5rem)] flex-wrap items-center gap-x-4 gap-y-1 rounded-md border border-border/55 bg-background/78 px-3 py-2 text-xs text-muted-foreground shadow-soft backdrop-blur-md sm:max-w-none">
              <span className="font-medium text-foreground">{labels.project}: {tree.project_id}</span>
              <span>{labels.nodes}: {tree.node_count}</span>
              <span>{labels.units}: {tree.unit_count}</span>
              <span>{labels.sources}: {tree.source_count}</span>
              {routed.selected.size > 0 && (
                <span className="text-amber-500 dark:text-amber-300">
                  {labels.routed}: {routed.selected.size}
                </span>
              )}
            </div>
            <svg
              role="tree"
              aria-label={`${labels.project} ${tree.project_id}`}
              width={width}
              height={height}
              viewBox={`0 0 ${width} ${height}`}
              className="max-w-none overflow-visible"
            >
              {[...positions.values()].map((position) => {
                const node = byId.get(position.id);
                if (!node) return null;
                const parent = position;
                const childPositions = (children.get(node.id) ?? [])
                  .map((id) => positions.get(id))
                  .filter((value): value is PositionedNode => Boolean(value));
                return childPositions.map((child) => {
                  const middle = (parent.y + child.y) / 2;
                  const highlighted = routed.path.has(node.id) && routed.path.has(child.id);
                  return (
                    <path
                      key={`${node.id}:${child.id}`}
                      d={`M ${parent.x} ${parent.y + 19} V ${middle} H ${child.x} V ${child.y - 19}`}
                      fill="none"
                      stroke="currentColor"
                      strokeWidth={highlighted ? 2 : 1}
                      className={highlighted ? "text-amber-400" : "text-border"}
                      opacity={highlighted ? 0.9 : 0.62}
                    />
                  );
                });
              })}
              {[...positions.values()].map((position) => {
                const node = byId.get(position.id);
                if (!node) return null;
                const key = nodeKey(tree.project_id, tree.tree_version, node.id);
                const isExpanded = expanded.has(key);
                const isRouted = routed.path.has(node.id);
                const isSelected = routed.selected.has(node.id);
                const radius = Math.min(16, 8 + Math.log2(Math.max(1, node.unit_count)));
                const childrenCount = children.get(node.id)?.length ?? 0;
                const ariaLabel = `${node.label}, ${labels.units}: ${node.unit_count}, ${labels.sources}: ${node.source_count}`;
                return (
                  <g
                    key={node.id}
                    role="treeitem"
                    aria-level={node.depth + 1}
                    aria-expanded={position.hasChildren ? isExpanded : undefined}
                    aria-selected={isSelected || selectedKey === key}
                    aria-label={ariaLabel}
                    tabIndex={0}
                    className="cursor-pointer outline-none"
                    onClick={() => {
                      setSelectedKey(key);
                      if (position.hasChildren) toggleNode(key);
                    }}
                    onKeyDown={(event) => {
                      if (event.key !== "Enter" && event.key !== " ") return;
                      event.preventDefault();
                      setSelectedKey(key);
                      if (position.hasChildren) toggleNode(key);
                    }}
                  >
                    <title>{`${ariaLabel}${position.hasChildren ? ` · ${isExpanded ? labels.collapse : labels.expand}` : ""}`}</title>
                    <circle
                      cx={position.x}
                      cy={position.y}
                      r={radius}
                      fill="currentColor"
                      className={isSelected
                        ? "text-amber-300"
                        : isRouted
                          ? "text-amber-500/80"
                          : selectedKey === key
                            ? "text-sky-400"
                            : "text-slate-400/80 dark:text-slate-500"}
                      stroke={selectedKey === key ? "currentColor" : "transparent"}
                      strokeWidth={5}
                    />
                    {position.hasChildren && (
                      <text
                        x={position.x}
                        y={position.y + 4}
                        textAnchor="middle"
                        className="fill-background text-[10px] font-bold"
                      >
                        {isExpanded ? "−" : "+"}
                      </text>
                    )}
                    <text
                      x={position.x + radius + 10}
                      y={position.y - 1}
                      className={`fill-foreground text-[12px] ${isRouted ? "font-semibold" : "font-medium"}`}
                    >
                      {node.label}
                    </text>
                    <text
                      x={position.x + radius + 10}
                      y={position.y + 17}
                      className="fill-muted-foreground text-[10px]"
                    >
                      {node.unit_count} {labels.units} · {node.source_count} {labels.sources}
                      {position.hasChildren && !isExpanded ? ` · ${childrenCount} ${labels.branches}` : ""}
                    </text>
                  </g>
                );
              })}
            </svg>
          </section>
        ))}
      </div>
    </div>
  );
}
