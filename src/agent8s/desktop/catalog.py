from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]

# The CLI accepts these aliases (verified live) and resolves them to the
# current model in each family, so this list doesn't go stale with releases.
CLAUDE_MODELS = [
    {"id": "", "label": "По умолчанию"},
    {"id": "opus", "label": "Opus"},
    {"id": "sonnet", "label": "Sonnet"},
    {"id": "haiku", "label": "Haiku"},
]

CODEX_FALLBACK_MODELS = [{"id": "", "label": "По умолчанию", "efforts": []}]

AGENT_LABELS = {"claude": "Claude Code", "codex": "Codex"}


def _codex_models(cache_path: Path) -> list[dict[str, Any]]:
    # codex keeps its own up-to-date model list (what the logged-in account
    # may actually use) in this cache; reading it avoids hardcoding names
    # that the next codex release renames.
    models: list[dict[str, Any]] = [{"id": "", "label": "По умолчанию", "efforts": []}]
    try:
        data = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        return CODEX_FALLBACK_MODELS
    raw = data.get("models", data) if isinstance(data, dict) else data
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or item.get("visibility") != "list" or not item.get("slug"):
            continue
        efforts = [
            level.get("effort")
            for level in item.get("supported_reasoning_levels", [])
            if isinstance(level, dict) and level.get("effort")
        ]
        models.append({"id": item["slug"], "label": item.get("display_name") or item["slug"], "efforts": efforts})
    return models


def build_catalog(codex_cache_path: Path | None = None) -> dict[str, Any]:
    """Agents available on this machine and the models/efforts each accepts."""
    cache = codex_cache_path or Path.home() / ".codex" / "models_cache.json"
    agents = []
    if shutil.which("claude"):
        agents.append(
            {
                "id": "claude",
                "label": AGENT_LABELS["claude"],
                "models": [{**m, "efforts": CLAUDE_EFFORTS} for m in CLAUDE_MODELS],
            }
        )
    if shutil.which("codex"):
        agents.append({"id": "codex", "label": AGENT_LABELS["codex"], "models": _codex_models(cache)})
    return {"agents": agents}
