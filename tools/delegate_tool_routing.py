"""Resolve operator-owned routing profiles for delegated children."""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional


def resolve_profile_route(config: Dict[str, Any], tool_profile: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return an isolated explicit route for ``tool_profile``, if configured.

    A profile route owns its provider, model, fallback policy, reasoning effort,
    and request overrides. It therefore does not inherit those keys from the
    general delegation block.
    """
    if not tool_profile or not isinstance(config, dict):
        return None
    routes = config.get("profile_routes")
    if not isinstance(routes, dict):
        return None
    route = routes.get(tool_profile)
    return copy.deepcopy(route) if isinstance(route, dict) else None
