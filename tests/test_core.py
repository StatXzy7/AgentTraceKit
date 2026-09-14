from pathlib import Path
import json
from agent_trace_kit.adapters.codex import CodexAdapter
from agent_trace_kit.bundle import create_bundle
from agent_trace_kit.html import render_timeline, render_annotation_workbench
from agent_trace_kit.verify import verify_bundle
from agent_trace_kit.corpus import index_corpus

def fixture(tmp_path):
 p=tmp_path/'rollout-test.jsonl'; lines=[{'timestamp':'2026-01-01T00:00:00Z','type':'session_meta','payload':{'session_id':'s1','cwd':'C:/demo','cli_version':'x'}},{'timestamp':'2026-01-01T00:00:01Z','type':'event_msg','payload':{'type':'user_message','message':'hello'}},{'timestamp':'2026-01-01T00:00:02Z','type':'response_item','payload':{'item':{'type':'message','role':'assistant','content':'<safe>'}}},{'timestamp':'2026-01-01T00:00:03Z','type':'response_item','payload':{'item':{'type':'function_call','call_id':'c1','name':'echo','arguments':'{}'}}},{'timestamp':'2026-01-01T00:00:04Z','type':'response_item','payload':{'item':{'type':'function_call_output','call_id':'c1','output':'ok'}}},{'type':'event_msg','payload':{'type':'task_complete'}},{'type':'new_future','payload':{'x':1}},'{bad']
 p.write_text('\n'.join(json.dumps(x) if isinstance(x,dict) else x for x in lines),encoding='utf-8'); return p

def test_parse_and_bundle(tmp_path):
 p=fixture(tmp_path); parsed=CodexAdapter().parse_session(p); assert len(parsed.events)>=6; assert parsed.warnings
 out=create_bundle(p,parsed,tmp_path/'out'); render_timeline(out); render_annotation_workbench(out); assert '<safe>' not in (out/'timeline.html').read_text(); assert verify_bundle(out)['bundle_integrity']=='ok'
 assert all(e.event_id.startswith('e_') for e in parsed.events)

def test_unknown_and_malformed(tmp_path):
    parsed=CodexAdapter().parse_session(fixture(tmp_path)); assert any(e.event_type=='unknown' for e in parsed.events)

def test_repeated_user_text_is_not_globally_deduped(tmp_path):
    p=tmp_path/'repeat.jsonl'; p.write_text('\n'.join([json.dumps({'type':'event_msg','payload':{'type':'user_message','message':'same'}}), json.dumps({'type':'response_item','payload':{'item':{'type':'message','role':'assistant','content':'ok'}}}), json.dumps({'type':'event_msg','payload':{'type':'user_message','message':'same'}})]), encoding='utf-8')
    assert len(CodexAdapter().parse_session(p).user_messages)==2

def test_annotation_files_and_corpus_index(tmp_path):
    p=fixture(tmp_path); parsed=CodexAdapter().parse_session(p); out=create_bundle(p,parsed,tmp_path/'out'); render_timeline(out); render_annotation_workbench(out)
    db,count=index_corpus(tmp_path/'out'); assert db.exists() and count==1
    assert (out/'annotation/ontology.json').exists() and (out/'annotation/annotations.jsonl').exists()
