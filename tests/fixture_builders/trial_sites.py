"""Synthetic fixtures for pipelines/trial_sites (RDS PostgreSQL, table clinical.sites as <entity>.csv). Deterministic.
Usage: python tests/fixture_builders/trial_sites.py [pipeline_repo_root, default: examples]"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

HEADER = "site_id,site_name,country,status,activated_on,enrolment_target,updated_at"


def rows(n: int) -> list[str]:
    out = []
    for i in range(1, n + 1):
        status = ["ACTIVE", "CLOSED", "PLANNED"][i % 3]
        activated = "" if status == "PLANNED" else f"2023-{1 + i % 12:02d}-15"
        out.append(f"{i:04d},Site {i},{['GB', 'SE', 'US', 'DE'][i % 4]},{status},{activated},{10 + i % 40},2024-03-01 00:{i % 60:02d}:00")
    return out


def with_field(line: str, idx: int, value: str) -> str:
    parts = line.split(",")
    parts[idx] = value
    return ",".join(parts)


def build(root: Path) -> None:
    fx = root / "pipelines" / "trial_sites" / "fixtures"
    for sub in ("good", "bad"):
        shutil.rmtree(fx / sub, ignore_errors=True)
    (fx / "good").mkdir(parents=True)
    base = rows(20)
    (fx / "good" / "sites.csv").write_text("\n".join([HEADER, *base]) + "\n")

    def case(name: str, lines: list[str], expect: str) -> None:
        d = fx / "bad" / name
        d.mkdir(parents=True)
        (d / "sites.csv").write_text("\n".join([HEADER, *lines]) + "\n")
        (d / "expect.yaml").write_text(expect.strip() + "\n")

    active = next(i for i, r in enumerate(base) if ",ACTIVE," in r)
    case("site_id_not_4_digits", [with_field(base[0], 0, "00001")] + base[1:], "status: loaded\nquarantined: {site_id_format: 1}")
    case("country_lower_case", [with_field(base[0], 2, "gb")] + base[1:], "status: loaded\nquarantined: {country_format: 1}")
    case("unknown_status", [with_field(base[0], 3, "OPEN")] + base[1:], "status: loaded\nquarantined: {status_values: 1}")
    case("target_out_of_range", [with_field(base[0], 5, "0")] + base[1:], "status: loaded\nquarantined: {enrolment_target_range: 1}")
    case("active_without_activation", [*base[:active], with_field(base[active], 4, ""), *base[active + 1:]],
         "status: loaded\nquarantined: {activated_on_required_when_active: 1}")
    case("empty_table", [], "rule_id: sites_has_rows")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "examples"))
