"""Phase 8: the runtime image definition is pinned, non-root, and its entry points exist in the code."""

from __future__ import annotations

import re
from pathlib import Path

from factory_runtime import lambda_handler, run

IMAGE = Path(__file__).resolve().parents[1] / "image" / "Dockerfile"
SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "build-runtime-image.sh"


def test_dockerfile_is_pinned_and_matches_the_entry_points() -> None:
    text = IMAGE.read_text()
    (base,) = re.findall(r"^FROM (\S+)", text, re.M)
    assert re.search(r"@sha256:[0-9a-f]{64}$", base), "the base image is pinned by digest"
    assert "--require-hashes" in text
    assert re.search(r"^USER 1000$", text, re.M)
    (cmd,) = re.findall(r'^CMD \["([\w.]+)"\]', text, re.M)
    module, func = cmd.rsplit(".", 1)
    assert module == lambda_handler.__name__
    assert callable(getattr(lambda_handler, func))
    assert callable(run.main)  # pods: python -m factory_runtime.run
    script = SCRIPT.read_text()
    assert "--frozen" in script  # the image installs exactly the lock
    assert "--no-emit-workspace" in script
