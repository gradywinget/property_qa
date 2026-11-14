"""End-to-end: real files in a temp folder, through the whole pipeline."""

import json
import shutil
from pathlib import Path

import pytest

from property_qa.__main__ import main
from property_qa.pipeline import run

ROOT = Path(__file__).resolve().parent.parent
HEADER = "Parcel ID,Street Address,City,State,Zip Code,Sale Price,Sale Date,Type,Beds,Baths,Sq Ft\n"


def good_row(pid="CA-100201", price="$500,000", date="2024-03-15"):
    return f"{pid},1 Main St,Fresno,CA,93721,\"{price}\",{date},Condo,2,1,900\n"


def write(folder: Path, name: str, text: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_sample_data_end_to_end(tmp_path):
    report = run(ROOT / "data" / "raw", tmp_path)
    t = report["totals"]
    assert report["status"] == "published"
    # Every input row is accounted for exactly once.
    assert t["clean"] + t["quarantined"] + t["exact_duplicates_dropped"] == t["rows_read"]
    clean = read_jsonl(tmp_path / "clean.jsonl")
    quarantine = read_jsonl(tmp_path / "quarantine.jsonl")
    assert len(clean) == t["clean"] and len(quarantine) == t["quarantined"]
    # Every quarantined row explains itself and keeps the original input.
    assert all(q["problems"] and "raw" in q and q["source"]["file"] for q in quarantine)
    # The upstream crash code was quarantined, never "patched".
    crash = [q for q in quarantine if q["raw"].get("closeDate") == "CRITICAL_TIMESTAMP_PARSE_ERROR"]
    assert crash and crash[0]["problems"][0]["code"] == "upstream_error_code"
    assert all(r["property_id"] != "OR-600101" for r in clean)


def test_same_input_same_output(tmp_path):
    run(ROOT / "data" / "raw", tmp_path / "a")
    run(ROOT / "data" / "raw", tmp_path / "b")
    for name in ("clean.jsonl", "quarantine.jsonl"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_clean_record_shape(tmp_path):
    write(tmp_path / "in", "a.csv", HEADER + good_row())
    run(tmp_path / "in", tmp_path / "out")
    [rec] = read_jsonl(tmp_path / "out" / "clean.jsonl")
    assert rec["purchase_price"] == "500000.00"  # Decimal serialized as an exact string
    assert rec["purchase_date"] == "2024-03-15"
    assert rec["_meta"]["source"] == {"file": "a.csv", "line": 2}
    assert rec["_meta"]["fixes"][0]["action"] == "removed_currency_formatting"


def test_format_change_is_held(tmp_path):
    report = run(ROOT / "data" / "format_change_demo", tmp_path)
    assert report["status"] == "held"
    assert not (tmp_path / "clean.jsonl").exists()


def test_day_first_evidence_withdraws_month_first_guesses(tmp_path):
    rows = good_row("CA-100201", date="05/02/2024") + good_row("CA-100202", date="13/05/2024") + good_row("CA-100203")
    write(tmp_path / "in", "a.csv", HEADER + rows)
    run(tmp_path / "in", tmp_path / "out", max_reject_rate=1.0)
    codes = {q["raw"]["Parcel ID"]: q["problems"][0]["code"] for q in read_jsonl(tmp_path / "out" / "quarantine.jsonl")}
    assert codes == {"CA-100201": "inconsistent_date_order", "CA-100202": "day_first_date"}
    assert [r["property_id"] for r in read_jsonl(tmp_path / "out" / "clean.jsonl")] == ["CA-100203"]


def test_day_first_evidence_is_per_file(tmp_path):
    write(tmp_path / "in", "a.csv", HEADER + good_row("CA-100201", date="05/02/2024"))
    write(tmp_path / "in", "b.csv", HEADER + good_row("CA-100202", date="13/05/2024"))
    run(tmp_path / "in", tmp_path / "out", max_reject_rate=1.0)
    assert [r["property_id"] for r in read_jsonl(tmp_path / "out" / "clean.jsonl")] == ["CA-100201"]


def test_conflicting_duplicates_all_quarantined(tmp_path):
    write(tmp_path / "in", "a.csv", HEADER + good_row(price="500000") + good_row(price="550000") + good_row("CA-100202"))
    report = run(tmp_path / "in", tmp_path / "out", max_reject_rate=1.0)
    assert report["totals"]["clean"] == 1
    assert report["reject_reasons"] == {"property_id: conflicting_duplicate": 2}


def test_exact_duplicates_kept_once(tmp_path):
    write(tmp_path / "in", "a.csv", HEADER + good_row() + good_row())
    report = run(tmp_path / "in", tmp_path / "out")
    assert report["totals"]["clean"] == 1 and report["totals"]["exact_duplicates_dropped"] == 1


def test_reject_rate_gate(tmp_path):
    write(tmp_path / "in", "a.csv", HEADER + good_row() + good_row("CA-100202", price="-1"))
    assert run(tmp_path / "in", tmp_path / "out", max_reject_rate=0.5)["status"] == "published"  # 50% is not ABOVE 50%
    assert run(tmp_path / "in", tmp_path / "out", max_reject_rate=0.4)["status"] == "held"


def test_held_run_removes_stale_clean_file(tmp_path):
    write(tmp_path / "in", "a.csv", HEADER + good_row())
    run(tmp_path / "in", tmp_path / "out")
    assert (tmp_path / "out" / "clean.jsonl").exists()
    write(tmp_path / "in", "a.csv", HEADER + good_row(price="-1"))
    assert run(tmp_path / "in", tmp_path / "out")["status"] == "held"
    assert not (tmp_path / "out" / "clean.jsonl").exists()


@pytest.mark.parametrize("name, content, error", [
    ("bad.json", '{"records": [ {"a": 1}, ', "invalid JSON"),
    ("empty.csv", "", "empty"),
    ("latin1.csv", None, "UTF-8"),
    ("dupe_header.csv", "id,id\n1,2\n", "duplicate column"),
])
def test_unreadable_file_holds_batch_without_crashing(tmp_path, name, content, error):
    write(tmp_path / "in", "good.csv", HEADER + good_row())
    if content is None:
        (tmp_path / "in" / name).write_bytes(HEADER.encode() + "CA-1,1 Mu\xf1oz St".encode("latin-1"))
    else:
        write(tmp_path / "in", name, content)
    report = run(tmp_path / "in", tmp_path / "out")
    assert report["status"] == "held"
    [failed] = [f for f in report["files"] if f["status"] == "failed"]
    assert failed["file"] == name and error in failed["error"]


def test_broken_jsonl_line_only_costs_that_line(tmp_path):
    good = '{"listing_id": "CO-700101", "address": "1 A St", "city": "Denver", "state": "CO", "zip": "80202", ' \
           '"price": "500000", "sold_date": "2024-04-01", "type": "condo"}'
    write(tmp_path / "in", "s.jsonl", good + '\n{"listing_id": "CO-7\n\n' + good.replace("700101", "700102") + "\n")
    report = run(tmp_path / "in", tmp_path / "out", max_reject_rate=1.0)
    assert report["totals"] == {**report["totals"], "rows_read": 3, "clean": 2, "quarantined": 1}


def test_unsupported_files_are_skipped_and_reported(tmp_path):
    write(tmp_path / "in", "a.csv", HEADER + good_row())
    write(tmp_path / "in", "notes.txt", "hello")
    report = run(tmp_path / "in", tmp_path / "out")
    assert report["status"] == "published"
    assert {"file": "notes.txt", "status": "skipped", "rows": 0, "error": "unsupported file type .txt"} in report["files"]


def test_empty_folder_is_held(tmp_path):
    (tmp_path / "in").mkdir()
    assert run(tmp_path / "in", tmp_path / "out")["status"] == "held"


def test_no_temp_files_left_behind(tmp_path):
    shutil.copytree(ROOT / "data" / "raw", tmp_path / "in")
    run(tmp_path / "in", tmp_path / "out")
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["clean.jsonl", "quarantine.jsonl", "report.json"]


def test_cli_exit_codes(tmp_path, capsys):
    assert main([str(ROOT / "data" / "raw"), "--out", str(tmp_path / "a")]) == 0
    assert main([str(ROOT / "data" / "format_change_demo"), "--out", str(tmp_path / "b")]) == 1
    assert "HOLD:" in capsys.readouterr().out
