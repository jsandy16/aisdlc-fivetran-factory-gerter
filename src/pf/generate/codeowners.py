"""Keep a managed CODEOWNERS block per pipeline, so each owner must approve the spec file they own."""
from __future__ import annotations

from pathlib import Path

from .. import yamlio
from ..bundle import OWNER_OF_FILE


def block(root: Path, name: str) -> str:
    p = yamlio.to_plain(yamlio.load(root / "pipelines" / name / "pipeline.yaml"))
    owners = p.get("owners", {})
    lines = [f"# pf:begin {name} (managed by `pf build`)"]
    for fname, role in OWNER_OF_FILE.items():
        lines.append(f"/pipelines/{name}/{fname} {owners.get(role, '')} {owners.get('data_engineer', '')}".rstrip())
    lines.append(f"/pipelines/{name}/rules/ {owners.get('data_engineer', '')} {owners.get('quality', '')}")
    lines.append(f"# pf:end {name}")
    return "\n".join(lines) + "\n"


def update(root: Path, name: str) -> None:
    f = root / ".github" / "CODEOWNERS"
    f.parent.mkdir(parents=True, exist_ok=True)
    text = f.read_text(encoding="utf-8") if f.exists() else "# Framework code is owned by the platform team.\n"
    start, end = f"# pf:begin {name} ", f"# pf:end {name}\n"
    if start in text:
        a = text.index(start)
        b_ = text.index(end, a) + len(end)
        text = text[:a] + block(root, name) + text[b_:]
    else:
        text = text.rstrip("\n") + "\n\n" + block(root, name)
    f.write_text(text, encoding="utf-8", newline="\n")
