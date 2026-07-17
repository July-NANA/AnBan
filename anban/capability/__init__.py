"""Tool, Skill, MCP, external Agent, and other executable capability adapters."""

from anban.capability.contracts import (
    ArtifactReference,
    CapabilityDescriptor,
    CapabilityHandler,
    CapabilityKind,
    CapabilityPort,
    CapabilityResult,
    CapabilityResultStatus,
    InvocationContext,
)
from anban.capability.local import local_capability_registry
from anban.capability.registry import CapabilityRegistry
from anban.capability.skill import (
    ApprovedSkill,
    SkillActivationCapability,
    SkillPackage,
    WorkspaceSkillCatalog,
    register_workspace_skill,
)

__all__ = [
    "ArtifactReference",
    "ApprovedSkill",
    "CapabilityDescriptor",
    "CapabilityHandler",
    "CapabilityKind",
    "CapabilityPort",
    "CapabilityRegistry",
    "CapabilityResult",
    "CapabilityResultStatus",
    "InvocationContext",
    "SkillActivationCapability",
    "SkillPackage",
    "WorkspaceSkillCatalog",
    "local_capability_registry",
    "register_workspace_skill",
]
