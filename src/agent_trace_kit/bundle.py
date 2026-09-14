import csv, hashlib, json, os, re, shutil
from pathlib import Path
from datetime import datetime, timezone
from .models import ParsedSession

def sha256(path):
    h=hashlib.sha256();
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()

def project_name(parsed):
    return Path(parsed.cwd).name if parsed.cwd else "unknown-project"

def create_bundle(source: Path, parsed: ParsedSession, output_root: Path) -> Path:
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S'); out=output_root/f"codex-{re.sub(r'[^A-Za-z0-9._-]+','-',project_name(parsed))}-{stamp}"; out.mkdir(parents=True,exist_ok=False)
    (out/'raw').mkdir(); (out/'trajectory').mkdir(); (out/'evidence').mkdir(); (out/'annotation').mkdir()
    raw_dest=out/'raw'/source.name; shutil.copyfile(source,raw_dest); source_hash=sha256(source)
    with (out/'trajectory/events.jsonl').open('w',encoding='utf-8',newline='\n') as f:
        for e in parsed.events: f.write(json.dumps(e.to_dict(),ensure_ascii=False)+'\n')
    interactions=[]; current=None
    for e in parsed.events:
        if e.event_type=='user_message':
            current={'interaction_id':f"i{len(interactions)+1:04d}", 'provider':e.provider,'session_id':e.session_id,'user_message':e.data.get('text',''),'assistant_messages':[],'tool_calls':[],'tool_results':[],'source_lines':[e.source_line], 'completion_facts':[]}
            interactions.append(current)
        elif current:
            current['source_lines'].append(e.source_line)
            if e.event_type=='assistant_message': current['assistant_messages'].append(e.data.get('content') or e.data.get('text') or e.data.get('message') or e.data.get('reasoning_summary') or '')
            elif e.event_type=='tool_call': current['tool_calls'].append(e.data)
            elif e.event_type=='tool_result': current['tool_results'].append(e.data)
            elif e.event_type=='lifecycle': current['completion_facts'].append(e.data)
    with (out/'trajectory/interactions.jsonl').open('w',encoding='utf-8') as f:
        for i in interactions: f.write(json.dumps(i,ensure_ascii=False)+'\n')
    with (out/'evidence/evidence.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f); w.writerow(['event_id','interaction_id','event_type','source_file','source_line','raw_type','sha256'])
        for e in parsed.events:
            iid='';
            for i in interactions:
                if e.source_line in i['source_lines']: iid=i['interaction_id']; break
            w.writerow([e.event_id,iid,e.event_type,e.source_file,e.source_line,e.raw_type,source_hash])
    with (out/'annotation/annotation_template.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f); w.writerow(['interaction_id','provider','user_message','assistant_response_preview','tool_calls','source_lines','completion_facts','review_status','error_type','severity','attribution','notes'])
        for i in interactions: w.writerow([i['interaction_id'],i['provider'],i['user_message'], ' '.join(map(str,i['assistant_messages']))[:500],len(i['tool_calls']),','.join(map(str,i['source_lines'])),json.dumps(i['completion_facts'],ensure_ascii=False),'pending','','','',''])
    manifest={'manifest_version':'1.0','provider':parsed.provider,'project':project_name(parsed),'session_id':parsed.session_id,'cwd':parsed.cwd,'timestamp':parsed.timestamp,'collection_time':datetime.now(timezone.utc).isoformat(),'source':{'name':source.name,'size':source.stat().st_size,'sha256':source_hash},'raw_copy':{'path':'raw/'+source.name,'size':raw_dest.stat().st_size,'sha256':sha256(raw_dest)},'event_count':len(parsed.events),'interaction_count':len(interactions),'parser_warning_count':len(parsed.warnings),'files':{}}
    for p in out.rglob('*'):
        if p.is_file() and p.name not in ('manifest.json','validation.json'): manifest['files'][str(p.relative_to(out)).replace('\\','/')]=sha256(p)
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    validation={'bundle_integrity':'ok','trajectory_parse_status':'ok' if not parsed.warnings else 'warnings','agent_run_status':'unknown','task_success':'unknown','warnings':[w.to_dict() for w in parsed.warnings]}
    (out/'validation.json').write_text(json.dumps(validation,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'README.txt').write_text(f"AgentTraceKit trajectory bundle\n\nThis folder is a portable copy of one Codex CLI session.\n\nraw/{source.name} is the untouched original JSONL bytes.\ntimeline.html is an offline readable report.\nannotation/annotation_template.csv is for human review.\nmanifest.json and validation.json record hashes and checks.\n\nPrivacy: review raw content before sharing or publishing.\n",encoding='utf-8')
    return out


