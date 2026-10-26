import type { UniverseKnowledgeUnit, UniverseTreeNode } from "@/lib/types";

export interface NeuralCortexRegionItem {
  key: string;
  project_id: string;
  tree_version: string;
  node: UniverseTreeNode;
  label: string;
  stats: string;
  isActive: boolean;
  isRouted: boolean;
  isMatched: boolean;
}

export interface NeuralCortexNeuronItem {
  key: string;
  clusterKey: string;
  unit: UniverseKnowledgeUnit;
  cleanLabel: string;
  isSelected: boolean;
  isMatched: boolean;
}

export interface NeuralCortexEdgeItem {
  id: string;
  sourceKey: string;
  targetKey: string;
  kind:
    | "cortical_tract"
    | "entity_commissure"
    | "cross_doc_axon"
    | "local_dendrite"
    | "ganglion_tether";
  weight: number;
  relationType?: string;
  sharedEntities?: string[];
}

export interface NeuralCortexSceneHandle {
  focusAll: () => void;
  focusNode: (key: string) => void;
}

export interface NeuralCortexSceneProps {
  regions: NeuralCortexRegionItem[];
  neurons: NeuralCortexNeuronItem[];
  edges: NeuralCortexEdgeItem[];
  selectedKey: string | null;
  activeRegionKey: string | null;
  filterMode: "all" | "cross_doc" | "e2_only";
  leftDrawerOpen?: boolean;
  rightDrawerOpen?: boolean;
  darkTheme: boolean;
  reducedMotion: boolean;
  onSelectRegion: (region: NeuralCortexRegionItem) => void;
  onSelectNeuron: (neuron: NeuralCortexNeuronItem) => void;
  onClearSelection: () => void;
}
