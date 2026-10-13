"""The run pin: the first task of a run records what the whole run uses; later tasks follow the pin, not the DAG.

A deploy during a run therefore cannot mix versions: a later task started with another bundle loads the pinned one,
and a different runtime image is refused (config, 78). `pinned_at` is the run's clock (run_date, now()).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from factory_core.errors import ConfigError

from .state import RunState

PIN = "PIN"


@dataclass(frozen=True)
class Pin:
    bundle_sha: str
    image_digest: str
    logic_sha: str
    binding_sha: str
    pinned_at: int  # epoch seconds (canonical JSON has no floats)


def establish_pin(state: RunState, run_dir: str, candidate: Pin) -> tuple[bool, Pin]:
    created, rec = state.put_if_absent(run_dir, PIN, asdict(candidate))
    doc = rec.doc
    return created, Pin(
        bundle_sha=str(doc["bundle_sha"]),
        image_digest=str(doc["image_digest"]),
        logic_sha=str(doc["logic_sha"]),
        binding_sha=str(doc["binding_sha"]),
        pinned_at=int(doc["pinned_at"]),
    )


def check_image(pin: Pin, image_digest: str) -> None:
    if pin.image_digest != image_digest:
        raise ConfigError(
            "run_pin_image_mismatch", "this run is pinned to another runtime image; retry with the pinned image"
        )
