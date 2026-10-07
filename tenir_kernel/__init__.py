"""Dependency-light TENIR execution kernel (schema 1.2)."""

from .kernel import KernelInputError, KernelPolicy, TenirKernel
from .schema import SCHEMA_VERSION, ENGINE_SPEC_VERSION

__all__ = [
    "ENGINE_SPEC_VERSION",
    "KernelInputError",
    "KernelPolicy",
    "SCHEMA_VERSION",
    "TenirKernel",
]

__version__ = "5.2.0"
