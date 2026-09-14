import json, uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ONTOLOGY={
    "ontology_version":"0.1",
    "labels":[
        {"name":"correct","description":"Meets the user's request"},
        {"name":"incomplete","description":"Leaves a requested part unfinished"},
        {"name":"tool_error","description":"Tool execution failed or returned an error"},
        {"name":"tool_result_mismatch","description":"Tool output does not match the call"},
        {"name":"unsupported_claim","description":"Response makes a claim without sufficient evidence"},
        {"name":"other","description":"Other reviewer-defined issue"}
    ],
    "review_statuses":["pending","accepted","rejected","needs_adjudication"]
}

def ensure_annotation_files(bundle: Path):
    d=bundle/'annotation'; d.mkdir(exist_ok=True)
    ontology=d/'ontology.json'
    if not ontology.exists(): ontology.write_text(json.dumps(DEFAULT_ONTOLOGY,ensure_ascii=False,indent=2),encoding='utf-8')
    annotations=d/'annotations.jsonl'
    annotations.touch(exist_ok=True)
    (d/'adjudication.jsonl').touch(exist_ok=True)
    return ontology, annotations

def make_annotation(bundle_id, interaction_id, label, event_ids=None, reviewer="", notes="", status="pending"):
    return {"annotation_id":"ann_"+uuid.uuid4().hex[:12],"bundle_id":bundle_id,"interaction_id":interaction_id,"event_ids":event_ids or [],"label":label,"reviewer":reviewer,"review_status":status,"severity":"","attribution":"","notes":notes,"ontology_version":DEFAULT_ONTOLOGY["ontology_version"],"created_at":datetime.now(timezone.utc).isoformat()}
