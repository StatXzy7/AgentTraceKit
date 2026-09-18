from __future__ import annotations
import csv, hashlib, json, shutil, uuid
from datetime import datetime, timezone
from pathlib import Path
from .bundle import sha256

POLICY = "pairwise_gsb_20260916"
def _now(): return datetime.now(timezone.utc).isoformat()
def create_pair(root: Path, task: str, prompt: str, provider: str, difficulty: str = ""):
    if difficulty and difficulty not in ("困难", "地狱", "hard", "hell"): raise ValueError("difficulty must be hard/hell or 困难/地狱")
    root.mkdir(parents=True, exist_ok=True); pair_id="pair_"+uuid.uuid4().hex[:12]; p=root/pair_id; p.mkdir()
    data={"schema_version":"1.0","policy":POLICY,"pair_id":pair_id,"task":task,"prompt":prompt,"provider":provider,"difficulty":difficulty,"created_at":_now(),"status":"draft","sides":{"A":{},"B":{}},"review":{}}
    (p/"pair.json").write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8"); return p
def load_pair(path): return json.loads((Path(path)/"pair.json").read_text(encoding="utf-8"))
def bind_side(path, side, session_id, bundle_path, source_kind="observed"):
    if side not in ("A","B"): raise ValueError("side must be A or B")
    p=Path(path); data=load_pair(p); other=data["sides"].get("B" if side=="A" else "A", {})
    if other.get("session_id") == session_id: raise ValueError("A and B must use different sessions")
    data["sides"][side]={"session_id":session_id,"bundle":str(Path(bundle_path).resolve()),"source_kind":source_kind,"bound_at":_now()}
    data["status"]="bound" if all(data["sides"].get(x) for x in ("A","B")) else "draft"
    (p/"pair.json").write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8"); return data
def import_review(path, review_path):
    p=Path(path); data=load_pair(p); review=json.loads(Path(review_path).read_text(encoding="utf-8")) if str(review_path).endswith(".json") else _read_review_toml(Path(review_path))
    if review.get("conclusion") not in ("A 更好","Same","B 更好",""): raise ValueError("conclusion must be A 更好, Same, or B 更好")
    data["review"]={**review,"imported_at":_now()}; data["status"]="reviewed"; (p/"pair.json").write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8"); return data
def _read_review_toml(path):
    import tomllib
    return tomllib.loads(path.read_text(encoding="utf-8"))
def export_pair(path, output, strict=False):
    d=load_pair(path); missing=[]
    for side in ("A","B"):
        if not d["sides"].get(side): missing.append(f"{side} binding")
    if not d.get("review",{}).get("conclusion"): missing.append("GSB conclusion")
    if strict and missing: raise ValueError("strict export missing: "+", ".join(missing))
    row=[d.get("prompt",""),d.get("task",""),d.get("difficulty",""),d.get("provider",""),"",d["sides"].get("A",{}).get("session_id",""),d["sides"].get("A",{}).get("bundle",""),d["sides"].get("B",{}).get("session_id",""),d["sides"].get("B",{}).get("bundle",""),d.get("review",{}).get("conclusion",""),d.get("review",{}).get("reason",""),"draft" if missing else "formal"]
    out=Path(output); out.parent.mkdir(parents=True,exist_ok=True)
    with out.open("w",encoding="utf-8-sig",newline="") as f: csv.writer(f).writerow(["User Prompt","任务类型","任务难度","Harness","Harness 版本","A-SessionID","A-轨迹文件","B-SessionID","B-轨迹文件","GSB 结论","GSB 理由","状态"]); csv.writer(f).writerow(row)
    return {"output":str(out),"draft":bool(missing),"missing":missing}
