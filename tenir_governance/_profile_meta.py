"""Profile metadata and disclosure for the Agentic Governance Profile.

Per the TENIR Versioning, Namespace & Governed Extension Policy v2:
  - These modules are Profile/Module namespace (§2.3), NOT canon.
  - They extend TENIR 2.0 on top of KB3; they do not modify KB3 or the schema.
  - Every exposed API must declare its authority scope (§6).

This module provides the disclosure metadata that consumers can inspect
to determine what semantic authority these modules carry (and do not carry).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProfileDisclosure:
    """Disclosure metadata per §6 of the Versioning Policy.

    Every exposed API, export, portal surface, or partner extension
    should declare: spec/doctrinal version, product version, contract
    version, compliance class, active profile(s), and the semantic
    authority it does or does not carry.
    """
    # Spec or doctrinal version these modules conform to
    doctrinal_version: str = "TENIR 2.0"
    # Schema version these modules conform to
    schema_version: str = "1.2"
    # Platform version these modules ship with
    platform_version: str = "5.6.1"
    # Profile/module version (separate from platform version per §2.3)
    profile_version: str = "agentic-governance-profile-v1.0.0"
    # Compliance class per KB3
    compliance_class: str = "profile-bound"
    # Active profiles
    active_profiles: tuple[str, ...] = ("agentic-governance",)
    # Semantic authority these modules do NOT carry
    semantic_authority_not_held: tuple[str, ...] = (
        "not canonical",
        "not doctrinal",
        "not constitutional",
    )
    # Semantic authority these modules DO carry
    semantic_authority_held: tuple[str, ...] = (
        "implementation-level governance enforcement",
        "profile-bound invariant checking",
    )


# Singleton instance for easy import
DISCLOSURE = ProfileDisclosure()


def get_disclosure() -> ProfileDisclosure:
    """Return the profile disclosure metadata."""
    return DISCLOSURE


__all__ = ["ProfileDisclosure", "DISCLOSURE", "get_disclosure"]