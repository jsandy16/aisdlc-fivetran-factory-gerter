"""Synthetic fixtures for pipelines/site_monitoring (FTPS, one CSV per delivery). Deterministic.
Usage: python tests/fixture_builders/site_monitoring.py [pipeline_repo_root, default: examples]"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

HEADER = "VISIT_ID,SITE_NO,STUDY,VISIT_TYPE,VISIT_DATE,REPORT_DATE,OPEN_ISSUES,CRA_EMAIL"


def rows(start: int, n: int) -> list[str]:
    out = []
    for i in range(start, start + n):
        vt = ["SIV", "IMV", "COV"][i % 3]
        out.append(f"MV{i:05d},{1000 + i % 7},D1234C00001,{vt},2024-03-{1 + i % 20:02d},2024-03-{3 + i % 20:02d},"
                   f"{0 if vt == 'COV' else i % 4},cra{i % 5}@cro.example")
    return out


def csv(lines: list[str]) -> bytes:
    return ("\n".join([HEADER, *lines]) + "\n").encode()


def with_field(line: str, idx: int, value: str) -> str:
    parts = line.split(",")
    parts[idx] = value
    return ",".join(parts)


def build(root: Path) -> None:
    fx = root / "pipelines" / "site_monitoring" / "fixtures"
    for sub in ("good", "bad"):
        shutil.rmtree(fx / sub, ignore_errors=True)
    (fx / "good").mkdir(parents=True)
    (fx / "good" / "SMV_20240301_01.csv").write_bytes(csv(rows(1, 30)))
    (fx / "good" / "SMV_20240301_02.csv").write_bytes(csv(rows(25, 10)))  # same-day re-issue: upsert by visit_id
    base = rows(1, 30)

    def case(name: str, files: dict[str, bytes], expect: str) -> None:
        d = fx / "bad" / name
        d.mkdir(parents=True)
        for f, data in files.items():
            (d / f).write_bytes(data)
        (d / "expect.yaml").write_text(expect.strip() + "\n")

    f = "SMV_20240301_01.csv"
    case("site_not_4_digits", {f: csv([with_field(base[0], 1, "10X1")] + base[1:])}, "status: loaded\nquarantined: {site_no_format: 1}")
    case("unknown_visit_type", {f: csv([with_field(base[0], 3, "XYZ")] + base[1:])}, "status: loaded\nquarantined: {visit_type_values: 1}")
    case("negative_issues", {f: csv([with_field(base[0], 6, "-1")] + base[1:])}, "status: loaded\nquarantined: {open_issues_non_negative: 1}")
    late = with_field(with_field(base[0], 4, "2024-03-01"), 5, "2024-03-20")
    case("report_too_late", {f: csv([late] + base[1:])}, "status: loaded\nquarantined: {report_within_10_days: 1}")
    case("cov_with_open_issues", {f: csv([base[0], with_field(base[1], 6, "3")] + base[2:])},
         "status: loaded\nquarantined: {cov_zero_open_issues: 1}")
    case("no_rows", {f: csv([])}, "status: rejected\nrule_id: site_monitoring_has_rows")
    case("empty_file", {f: b""}, "status: rejected\nrule_id: package_not_empty")
    case("truncated_file", {f: b"VISIT_ID,SITE_NO,STUDY\n"}, "status: rejected\nrule_id: package_size")
    case("misnamed_file", {"SMV_2024031_1.csv": csv(base)}, "status: rejected\nrule_id: package_name")
    case("same_content_twice", {f: csv(base), "SMV_20240302_01.csv": csv(base)}, "status: rejected\nrule_id: package_not_duplicate")
    case("stray_file", {"notes.txt": b"call the CRA\n"}, "status: unmatched\nrule_id: package_match")
    case("unexpected_column", {f: (HEADER + ",EXTRA\n" + "\n".join(r + ",x" for r in base) + "\n").encode()}, "status: rejected")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "examples"))
