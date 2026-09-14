import csv, hashlib, json
from pathlib import Path
from .bundle import sha256
def verify_bundle(path: Path):
    issues=[]; mpath=path/'manifest.json'
    required=['raw','trajectory/events.jsonl','trajectory/interactions.jsonl','evidence/evidence.csv','annotation/annotation_template.csv','annotation/ontology.json','annotation/annotations.jsonl','annotation/adjudication.jsonl','annotation/workbench.html','timeline.html','README.txt','validation.json']
    for rel in required:
        if not (path/rel).exists(): issues.append(f'missing required file: {rel}')
    if not mpath.exists(): return {'bundle_integrity':'invalid','issues':['manifest.json missing']}
    m=json.loads(mpath.read_text(encoding='utf-8'))
    for rel,digest in m.get('files',{}).items():
        p=path/rel
        if not p.exists(): issues.append(f'missing file: {rel}')
        elif sha256(p)!=digest: issues.append(f'hash mismatch: {rel}')
    raw=path/m.get('raw_copy',{}).get('path','')
    if not raw.exists(): issues.append('raw copy missing')
    elif sha256(raw)!=m.get('raw_copy',{}).get('sha256'): issues.append('raw hash mismatch')
    max_line=None
    try:
        max_line=sum(1 for _ in raw.open('rb')) if raw.exists() else None
        for row in csv.DictReader((path/'evidence/evidence.csv').open(encoding='utf-8-sig')):
            line=int(row['source_line']);
            if line<1 or (max_line is not None and line>max_line): issues.append(f'invalid source line: {line}')
    except Exception as e: issues.append(f'evidence.csv invalid: {e}')
    validation={}
    try: validation=json.loads((path/'validation.json').read_text(encoding='utf-8'))
    except Exception: issues.append('validation.json invalid')
    return {'bundle_integrity':'invalid' if issues else 'ok','trajectory_parse_status':validation.get('trajectory_parse_status','unknown'),'agent_run_status':validation.get('agent_run_status','unknown'),'task_success':validation.get('task_success','unknown'),'issues':issues}
