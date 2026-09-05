"""Integrated local-first intelligence services for KAZUMI.

The platform export is lazy so sibling logical domains can depend on the
storage/memory modules without importing the full runtime graph.
"""

from typing import Any

__all__ = ["IntelligencePlatform"]


def __getattr__(name: str) -> Any:
    if name == "IntelligencePlatform":
        from app.intelligence.service import IntelligencePlatform

        return IntelligencePlatform
    raise AttributeError(name)
