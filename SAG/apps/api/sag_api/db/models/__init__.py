"""ORM models aggregate import — ensures Base.metadata registers all tables."""

from sag_api.db.models.agent import Agent, AgentBinding, Message, Thread
from sag_api.db.models.document import Document
from sag_api.db.models.job import Job
from sag_api.db.models.knowledge import (
    KnowledgeEvidence,
    KnowledgeGraphBuild,
    KnowledgeJob,
    KnowledgeQueueControl,
    KnowledgeTreeNode,
    KnowledgeUnit,
    KnowledgeUnitEdge,
)
from sag_api.db.models.octx import (
    OctxAsset,
    OctxDocumentBinding,
    OctxInstallation,
    OctxOperationLease,
    OctxRelease,
    OctxSourceBinding,
    OctxTransfer,
)
from sag_api.db.models.routing_rag import (
    CanonicalBlock,
    DocumentVersion,
    IngestionRun,
    KnowledgeGraphEdge,
    ProjectSearchState,
    SearchUnit,
    SourceSnapshot,
    StageRun,
    TreeManifest,
    TreeRoutingProfile,
    TreeSnapshotLease,
)
from sag_api.db.models.setting import Setting
from sag_api.db.models.source import Source
from sag_api.db.models.source_project_mapping import SourceProjectMapping
from sag_api.db.models.universe import (
    ExplorationSession,
    ExplorationStep,
    UniverseDirtySource,
    UniverseOverview,
    UniversePartition,
)
from sag_api.db.models.user import User

__all__ = [
    "Agent",
    "AgentBinding",
    "CanonicalBlock",
    "Document",
    "DocumentVersion",
    "ExplorationSession",
    "ExplorationStep",
    "IngestionRun",
    "Job",
    "KnowledgeGraphEdge",
    "KnowledgeEvidence",
    "KnowledgeGraphBuild",
    "KnowledgeJob",
    "KnowledgeQueueControl",
    "KnowledgeTreeNode",
    "KnowledgeUnit",
    "KnowledgeUnitEdge",
    "Message",
    "OctxAsset",
    "OctxDocumentBinding",
    "OctxInstallation",
    "OctxOperationLease",
    "OctxRelease",
    "OctxSourceBinding",
    "OctxTransfer",
    "ProjectSearchState",
    "SearchUnit",
    "Setting",
    "Source",
    "SourceProjectMapping",
    "SourceSnapshot",
    "StageRun",
    "Thread",
    "TreeManifest",
    "TreeRoutingProfile",
    "TreeSnapshotLease",
    "UniverseDirtySource",
    "UniverseOverview",
    "UniversePartition",
    "User",
]


