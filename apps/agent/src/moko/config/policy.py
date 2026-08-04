"""Per-category tool trust, persisted under ~/.moko.

The consent gate's standing policy: a map of tool category → "allow" | "ask" |
"deny". Empty by default (so the risk floor applies: safe→allow, sensitive→ask).
The companion raises or pins trust by approving with "always", or via
`moko trust`.
"""

from __future__ import annotations

import json

from .paths import moko_home


def _path():
    return moko_home() / "tool_policy.json"


def load_policy() -> dict[str, str]:
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def set_policy(category: str, decision: str) -> None:
    policy = load_policy()
    policy[category] = decision
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)  # ~/.moko may not exist yet
    path.write_text(json.dumps(policy, indent=2), encoding="utf-8")
