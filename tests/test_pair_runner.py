from pathlib import Path
from agent_trace_kit.pair_runner import PairSpec, export_dataset, run_pair, validate_spec


def repo(path: Path):
    (path / ".git").mkdir(); (path / "go.mod").write_text("module demo\n", encoding="utf-8")


def test_validation_reports_missing_fields(tmp_path):
    checks = validate_spec(PairSpec())
    assert any(not x["ok"] and x["name"] == "prompt" for x in checks)
    assert any(not x["ok"] and x["name"] == "A directory" for x in checks)


def test_run_pair_and_export(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"; a.mkdir(); b.mkdir(); repo(a); repo(b)
    spec = PairSpec(prompt="same", environment="go version", a_directory=str(a), b_directory=str(b), winner="Same", rationale="same", commands=["python -c \"print(1)\""])
    result = run_pair(spec, tmp_path / "out")
    assert result["ok"] and result["A"]["ok"] and result["B"]["ok"]
    csv = export_dataset(spec, result, tmp_path / "out" / "dataset.csv")
    assert csv.exists() and "A_check_status" in csv.read_text(encoding="utf-8-sig")
