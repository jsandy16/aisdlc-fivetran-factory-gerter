"""`factory secrets template`: the developer's local secrets file with every name and key the pipeline needs, empty.

Values are never written, printed or read back by this command. An existing file is merged: values already filled in
stay, missing entries are added. The file is created private to its owner (0600), outside the repository.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from factory_core.envelope import CommandError
from factory_core.templates import parse_template, render
from factory_spec.repo import Repo
from factory_spec.secrets import local_secrets_path

from factory_core import yamlio

HEADER = (
    "# Local secrets for `factory run` (development only). Never commit this file, never paste it anywhere.\n"
    "# Fill in the empty values; keys follow each secret's path in Vault or Secrets Manager.\n"
)


def required_entries(spec: Any, env: str) -> dict[str, Any]:
    tokens = {"env": env, "team": spec.pipeline.metadata.team, "pipeline_id": spec.pipeline_id}
    out: dict[str, Any] = {}
    for _name, ref in sorted(spec.integrations.secrets.items()):
        path = render(parse_template(ref.path), tokens, path_safe=True)
        if ref.key is None:
            out.setdefault(path, "")
        else:
            entry = out.get(path)
            out[path] = {**(entry if isinstance(entry, dict) else {}), ref.key: ""}
    return out


def write_template(spec: Any, repo: Repo, env: str, path: Path | None = None) -> dict[str, Any]:
    ps = repo.platform.get(env)
    if ps is None or not ps.allow_local_secrets:
        raise CommandError(f"the platform does not allow local secrets in '{env}' (allow_local_secrets)", "security")
    target = path or local_secrets_path(spec.pipeline.metadata.team, env)
    existing: dict[str, Any] = {}
    if target.is_file():
        loaded = yamlio.to_plain(yamlio.load_path(target))
        existing = loaded if isinstance(loaded, dict) else {}
    merged = dict(existing)
    added: list[str] = []
    for p, want in required_entries(spec, env).items():
        have = merged.get(p)
        if isinstance(want, dict):
            have = have if isinstance(have, dict) else {}
            for k in want:
                if k not in have:
                    have[k] = ""
                    added.append(f"{p}#{k}")
            merged[p] = have
        elif p not in merged:
            merged[p] = ""
            added.append(p)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(HEADER + yamlio.dump_text(dict(sorted(merged.items()))))
    os.chmod(target, 0o600)
    empty = sum(1 for v in merged.values() for x in (v.values() if isinstance(v, dict) else [v]) if x in ("", None))
    return {"file": str(target), "added": added, "entries": len(merged), "still_empty": empty}
