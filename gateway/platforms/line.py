"""Builtin import path for the LINE adapter.

``_BUILTIN_ADAPTERS`` lazy-imports ``gateway.platforms.line``. The adapter
itself lives in the bundled ``line-platform`` plugin.
"""

from plugins.platforms.line.adapter import LineAdapter, check_requirements

__all__ = ["LineAdapter", "check_requirements"]
