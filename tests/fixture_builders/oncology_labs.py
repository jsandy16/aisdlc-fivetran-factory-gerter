"""Builds the synthetic fixtures for pipelines/oncology_labs (never real data). Deterministic: re-running
produces byte-identical files. Usage: python tests/fixture_builders/oncology_labs.py [pipeline_repo_root, default: examples]"""
from __future__ import annotations

import hashlib
import io
import shutil
import sys
import zipfile
from pathlib import Path

HEADER = "SUBJID,SITEID,VISIT,VISITDT,RCVDT,LBTESTCD,LBORRES,LBORRESU"
TESTS = ["ALT", "AST", "HGB", "WBC", "PLAT", "CREAT"]
UNITS = {"ALT": "U/L", "AST": "U/L", "HGB": "g/dL", "WBC": "10^9/L", "PLAT": "10^9/L", "CREAT": "mg/dL"}


def rows(n_subjects: int = 4, visits: int = 2) -> list[str]:
    out = []
    for s in range(1, n_subjects + 1):
        for v in range(1, visits + 1):
            for i, t in enumerate(TESTS):
                out.append(f"S{s:04d},{12 + s},{v},0{v}MAR2024,0{v + 2}MAR2024, {t.lower()} ,{10 + s + v + i}.5,{UNITS[t]}")
    return out


def csv_bytes(lines: list[str]) -> bytes:
    return ("\n".join([HEADER, *lines]) + "\n").encode()


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=(2024, 3, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, members[name])
    return buf.getvalue()


def delivery(lines: list[str] | None = None, ctrl: int | None = None, extra: dict[str, bytes] | None = None,
             drop: tuple[str, ...] = ()) -> bytes:
    lines = rows() if lines is None else lines
    m = {"lab_results_001.csv": csv_bytes(lines), "LB_CTRL.txt": f"ROWS={len(lines) if ctrl is None else ctrl}\n".encode()}
    m.update(extra or {})
    for d in drop:
        m.pop(d, None)
    return zip_bytes(m)


def put(case: Path, rel: str, data: bytes, sidecar: str | None = "auto") -> None:
    p = case / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if sidecar is not None:
        digest = hashlib.sha256(data).hexdigest() if sidecar == "auto" else sidecar
        (p.parent / f"{p.name}.sha256").write_text(f"{digest}  {p.name}\n")


def expect(case: Path, text: str) -> None:
    (case / "expect.yaml").write_text(text.strip() + "\n")


def build(root: Path) -> None:
    fx = root / "pipelines" / "oncology_labs" / "fixtures"
    for sub in ("good", "bad"):
        shutil.rmtree(fx / sub, ignore_errors=True)
    good = fx / "good"
    put(good, "20240301/LB_ONC101_001.zip", delivery())
    second = rows(2, 3)[24:]  # visit 3 for subjects 1-2 → new keys
    put(good, "20240302/LB_ONC101_002.zip", delivery(second))

    bad = fx / "bad"
    c = bad / "empty_package"; put(c, "20240301/LB_ONC101_001.zip", b"")
    expect(c, "status: rejected\nrule_id: package_not_empty")
    c = bad / "too_small"; put(c, "20240301/LB_ONC101_001.zip", b"PK" + b"x" * 40)
    expect(c, "status: rejected\nrule_id: package_size")
    c = bad / "duplicate_content"; d = delivery()
    put(c, "20240301/LB_ONC101_001.zip", d); put(c, "20240302/LB_ONC101_002.zip", d)
    expect(c, "status: rejected\nrule_id: package_not_duplicate")
    c = bad / "bad_file_name"; put(c, "20240301/lb_onc101_1.zip", delivery())
    expect(c, "status: rejected\nrule_id: package_name")
    c = bad / "bad_checksum"; put(c, "20240301/LB_ONC101_001.zip", delivery(), sidecar="0" * 64)
    expect(c, "status: rejected\nrule_id: package_checksum")
    c = bad / "missing_checksum"; put(c, "20240301/LB_ONC101_001.zip", delivery(), sidecar=None)
    expect(c, "status: rejected\nrule_id: package_checksum")
    c = bad / "not_a_zip"; put(c, "20240301/LB_ONC101_001.zip", csv_bytes(rows()))
    expect(c, "status: rejected\nrule_id: package_is_zip")
    c = bad / "bad_folder"; put(c, "2024-03-01/LB_ONC101_001.zip", delivery())
    expect(c, "status: rejected\nrule_id: package_folder")
    c = bad / "missing_control"; put(c, "20240301/LB_ONC101_001.zip", delivery(drop=("LB_CTRL.txt",)))
    expect(c, "status: rejected\nrule_id: package_members")
    c = bad / "executable_member"; put(c, "20240301/LB_ONC101_001.zip", delivery(extra={"run.sh": b"echo hi\n"}))
    expect(c, "status: rejected\nrule_id: package_no_executables")
    c = bad / "too_many_members"
    put(c, "20240301/LB_ONC101_001.zip", delivery(extra={f"notes_{i:02d}.txt": b"n\n" for i in range(19)}))
    expect(c, "status: rejected\nrule_id: package_member_count")
    c = bad / "zip_slip"; put(c, "20240301/LB_ONC101_001.zip", delivery(extra={"../../evil.txt": b"x"}))
    expect(c, "status: rejected")

    base = rows()
    def row_case(name: str, idx: int, field: int, value: str, rule: str, n: int = 1) -> None:
        lines = list(base)
        for k in range(n):
            parts = lines[idx + k].split(",")
            parts[field] = value
            lines[idx + k] = ",".join(parts)
        c = bad / name
        put(c, "20240301/LB_ONC101_001.zip", delivery(lines))
        expect(c, f"status: loaded\nrule_id: {rule}\nquarantined: {{{rule}: {n}}}")
    row_case("site_id_too_long", 0, 1, "12345", "site_id_format")
    row_case("visit_out_of_range", 1, 2, "150", "visit_range")
    row_case("unknown_test_code", 2, 5, "XYZ", "test_code_known")
    row_case("result_without_unit", 3, 7, "", "result_has_unit")
    row_case("received_before_visit", 4, 4, "01JAN2024", "visit_before_receipt")

    c = bad / "units_missing_above_threshold"; lines = list(base)
    for k in range(0, 10):
        lines[k] = ",".join(lines[k].split(",")[:7] + [""])
    put(c, "20240301/LB_ONC101_001.zip", delivery(lines))
    expect(c, "status: rejected\nrule_id: result_has_unit")
    c = bad / "no_rows"; put(c, "20240301/LB_ONC101_001.zip", delivery([], ctrl=0))
    expect(c, "status: rejected\nrule_id: lab_results_has_rows")
    c = bad / "duplicate_key"; put(c, "20240301/LB_ONC101_001.zip", delivery(base + [base[0]]))
    expect(c, "status: rejected\nrule_id: lab_results_unique")
    c = bad / "control_count_mismatch"; put(c, "20240301/LB_ONC101_001.zip", delivery(ctrl=len(base) + 1))
    expect(c, "status: rejected\nrule_id: lab_results_control_count")
    c = bad / "unexpected_column"
    m = {"lab_results_001.csv": (HEADER + ",EXTRA\n" + "\n".join(r + ",x" for r in base) + "\n").encode(),
         "LB_CTRL.txt": f"ROWS={len(base)}\n".encode()}
    put(c, "20240301/LB_ONC101_001.zip", zip_bytes(m))
    expect(c, "status: rejected")
    c = bad / "bad_date_format"; lines = list(base); p = lines[5].split(","); p[3] = "2024-03-01"; lines[5] = ",".join(p)
    put(c, "20240301/LB_ONC101_001.zip", delivery(lines))
    expect(c, "status: loaded\nquarantined: {type__visit_date: 1}")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "examples"))
