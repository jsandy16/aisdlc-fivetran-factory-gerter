"""Helpers for the agent tests: a runner that turns scripted answers into recordings, then replays them.

Every answer goes through the real replay path (key, recording file, tamper check, schema validation). The answers
are hand-written and synthetic; provenance says so.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from factory_agents.runner import AgentRunner, AgentSpec, Provenance, RunnerConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
UNIT_REPO = REPO_ROOT / "packages" / "factory_runtime" / "tests" / "data" / "unit_repo"
PLATFORM_SETTINGS = REPO_ROOT / "platform" / "platform-settings"


class Scripted(AgentRunner):
    def __init__(self, recordings: Path, script: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__(RunnerConfig(mode="replay", recordings=recordings))
        self.script = {k: list(v) for k, v in script.items()}
        self.prompts: dict[str, list[str]] = {}

    def run(self, agent: AgentSpec[Any], prompt: str) -> tuple[Any, Provenance]:
        self.prompts.setdefault(agent.name, []).append(prompt)
        if not self.recording_path(agent, prompt).is_file():
            answers = self.script.get(agent.name) or []
            if not answers:
                raise AssertionError(f"no scripted answer left for {agent.name}")
            self.save(agent, prompt, answers.pop(0), {"provider": "synthetic", "provider_model": "hand-written",
                                                      "cost_usd": 0.01})  # fmt: skip
        return super().run(agent, prompt)


def copy_unit_repo(dest: Path) -> Path:
    shutil.copytree(UNIT_REPO, dest, ignore=shutil.ignore_patterns("__pycache__"))
    (dest / "factory.toml").write_text(
        f'api_version = "factory/v1"\nteam = "clinops_data"\nenvironments = ["dev"]\n'
        f'platform_settings = "{PLATFORM_SETTINGS.as_posix()}"\n',
        encoding="utf-8",
    )
    return dest
