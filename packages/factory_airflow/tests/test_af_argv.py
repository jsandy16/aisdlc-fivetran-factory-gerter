"""Pure helpers of the operators (no Airflow needed): the pod exit code read from a finished pod."""

from __future__ import annotations

from types import SimpleNamespace as Ns

from factory_airflow.argv import pod_exit_code


def _pod(name: str, code: int | None) -> Ns:
    state = Ns(terminated=Ns(exit_code=code) if code is not None else None)
    return Ns(status=Ns(container_statuses=[Ns(name="istio-proxy", state=Ns(terminated=Ns(exit_code=0))),
                                            Ns(name=name, state=state)]))  # fmt: skip


def test_pod_exit_code() -> None:
    assert pod_exit_code(_pod("base", 65), "base") == 65
    assert pod_exit_code(_pod("base", None), "base") is None
    assert pod_exit_code(_pod("other", 65), "base") is None
    assert pod_exit_code(Ns(status=None), "base") is None
