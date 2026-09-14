import json, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote
from .adapters.codex import CodexAdapter
from .bundle import create_bundle, refresh_manifest
from .html import render_timeline, render_annotation_workbench
from .verify import verify_bundle
from .annotation import ensure_annotation_files

def session_records(adapter=None):
    adapter=adapter or CodexAdapter(); records=[]
    for p in adapter.discover_sessions():
        try:
            info=adapter.inspect_session(p); info['size']=p.stat().st_size; info['path']=str(p.resolve()); records.append(info)
        except OSError: continue
    return records

PAGE="""<!doctype html><meta charset='utf-8'><title>AgentTraceKit Sessions</title>
<style>
:root{font:15px system-ui;color:#1f2937;background:#f4f6f8}*{box-sizing:border-box}body{margin:0}header{height:64px;background:#fff;border-bottom:1px solid #d9dee7;display:flex;align-items:center;padding:0 24px;gap:18px;position:sticky;top:0;z-index:2}header b{font-size:19px}header span{color:#64748b;font-size:13px}.search{margin-left:auto;width:min(420px,45vw);padding:10px 14px;border:1px solid #cbd5e1;border-radius:8px}.layout{display:grid;grid-template-columns:minmax(340px,43%) 1fr;min-height:calc(100vh - 64px)}aside{background:#fff;border-right:1px solid #d9dee7;padding:16px;overflow:auto}.filters{display:flex;gap:8px;margin-bottom:12px}.filters select{padding:8px;border:1px solid #cbd5e1;border-radius:6px}.session{width:100%;text-align:left;border:1px solid #e2e8f0;background:#fff;border-radius:8px;padding:13px;margin:8px 0;cursor:pointer}.session:hover,.session.active{border-color:#2563eb;box-shadow:0 0 0 2px #dbeafe}.session strong{display:block;font-size:15px}.session small{display:block;color:#64748b;margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.badge{float:right;color:#2563eb;font-size:11px;font-weight:600}.detail{padding:28px;max-width:900px}.hero{background:#fff;border:1px solid #d9dee7;border-radius:10px;padding:22px}.hero h1{margin:0 0 4px;font-size:24px}.muted{color:#64748b}.stats{display:flex;gap:24px;margin:18px 0}.stat b{display:block;font-size:20px}.prompt{white-space:pre-wrap;background:#f8fafc;padding:12px;border-radius:7px;max-height:180px;overflow:auto}.primary{background:#2563eb;color:#fff;border:0;border-radius:7px;padding:11px 18px;font-weight:600;cursor:pointer}.primary:disabled{opacity:.6}.status{margin-left:12px;color:#475569}.empty{color:#64748b;text-align:center;padding:60px 20px}@media(max-width:760px){.layout{grid-template-columns:1fr}.detail{padding:16px}aside{max-height:48vh}.search{width:40vw}}
</style><header><b>AgentTraceKit</b><span>Local · Private · Codex sessions</span><input id='search' class='search' placeholder='Search projects or prompts…'><button onclick='load()'>↻ Refresh</button></header><div class='layout'><aside><div class='filters'><select id='project' onchange='render()'><option value=''>All projects</option></select><select id='days' onchange='render()'><option value='0'>All dates</option><option value='7'>Last 7 days</option><option value='30'>Last 30 days</option></select></div><div id='list'></div></aside><section id='detail' class='detail'><div class='empty'>Select a Codex session to review it here.</div></section></div>
<script>
let sessions=[],selected=null;const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){let r=await fetch('/api/sessions');sessions=await r.json();let ps=[...new Set(sessions.map(s=>(s.cwd||'unknown').split(/[\\/]/).pop()))].sort();project.innerHTML='<option value="">All projects</option>'+ps.map(p=>`<option>${esc(p)}</option>`).join('');render()}
function render(){let q=search.value.toLowerCase(),p=project.value,d=+days.value;let now=Date.now()/1000;let xs=sessions.filter(s=>(!q||JSON.stringify(s).toLowerCase().includes(q))&&(!p||(s.cwd||'').endsWith(p))&&(!d||now-(Date.parse(s.timestamp||s.mtime)/1000)<=d*86400));list.innerHTML=xs.map((s,i)=>`<button class='session ${selected&&selected.path===s.path?'active':''}' onclick='select(${sessions.indexOf(s)})'><span class='badge'>CODEX</span><strong>${esc((s.cwd||'unknown').split(/[\\/]/).pop())}</strong><small>${esc(s.timestamp||s.mtime||'Unknown time')}</small><small>${esc(s.preview||'(no user-message preview)')}</small></button>`).join('')||'<div class="empty">No sessions match this search.</div>'}
function select(i){selected=sessions[i];render();detail.innerHTML=`<div class='hero'><span class='badge'>CODEX</span><h1>${esc((selected.cwd||'unknown').split(/[\\/]/).pop())}</h1><div class='muted'>${esc(selected.timestamp||selected.mtime||'Unknown time')} · ${esc(selected.cwd||'Unknown folder')}</div><div class='stats'><div class='stat'><b>${(selected.size/1024).toFixed(1)} KB</b><span class='muted'>raw file</span></div><div class='stat'><b>${esc(String(selected.session_id||'').slice(0,12))}</b><span class='muted'>session ID</span></div></div><h3>First prompt</h3><div class='prompt'>${esc(selected.preview||'(No preview available)')}</div><p><button class='primary' id='collect' onclick='collect()'>Collect &amp; Review</button><span id='status' class='status'></span></p><details><summary>Advanced metadata</summary><p class='muted'>Source: ${esc(selected.path)}<br>Session ID: ${esc(selected.session_id||'unknown')}</p></details></div>`}
async function collect(){collect.disabled=true;status.textContent='Collecting and verifying…';let r=await fetch('/api/collect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:selected.path})});let x=await r.json();if(!r.ok){status.textContent='✗ '+x.error;collect.disabled=false;return}status.textContent='✓ Bundle ready';setTimeout(()=>location.href=x.report_url,350)}
search.oninput=render;load();
</script>"""

def dashboard_page(bundle: Path):
    manifest=json.loads((bundle/'manifest.json').read_text(encoding='utf-8'))
    validation=json.loads((bundle/'validation.json').read_text(encoding='utf-8'))
    interactions=[json.loads(x) for x in (bundle/'trajectory/interactions.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()]
    events=[json.loads(x) for x in (bundle/'trajectory/events.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()]
    ontology=json.loads((bundle/'annotation/ontology.json').read_text(encoding='utf-8')) if (bundle/'annotation/ontology.json').exists() else {'labels':[]}
    annotations=[json.loads(x) for x in (bundle/'annotation/annotations.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()] if (bundle/'annotation/annotations.jsonl').exists() else []
    evidence=[]
    try:
        import csv
        with (bundle/'evidence/evidence.csv').open(encoding='utf-8-sig') as f: evidence=list(csv.DictReader(f))
    except Exception: pass
    data=json.dumps({'manifest':manifest,'validation':validation,'interactions':interactions,'events':events,'ontology':ontology,'annotations':annotations,'evidence':evidence},ensure_ascii=False).replace('</','<\\/')
    return """<!doctype html><meta charset='utf-8'><title>AgentTraceKit · Review</title><style>
:root{font:15px system-ui;color:#1f2937;background:#f4f6f8}*{box-sizing:border-box}body{margin:0}header{height:64px;background:#fff;border-bottom:1px solid #d9dee7;display:flex;align-items:center;padding:0 22px;gap:14px;position:sticky;top:0;z-index:3}header b{font-size:19px}button{padding:8px 12px;border:1px solid #cbd5e1;border-radius:6px;background:#fff;cursor:pointer}.primary{background:#2563eb;color:#fff;border:0}.layout{display:grid;grid-template-columns:210px 1fr;min-height:calc(100vh - 64px)}nav{background:#fff;border-right:1px solid #d9dee7;padding:14px}nav button{width:100%;text-align:left;margin:3px 0}.content{padding:24px;max-width:1100px}.tab{display:none}.tab.active{display:block}.hero,.card{background:#fff;border:1px solid #d9dee7;border-radius:9px;padding:18px;margin-bottom:14px}.hero h1{margin:0 0 4px}.muted{color:#64748b}.stats{display:flex;gap:28px;margin:18px 0}.stat b{display:block;font-size:21px}.event{background:#fff;border:1px solid #e2e8f0;border-radius:7px;padding:10px;margin:7px 0;border-left:4px solid #94a3b8}.event.user_message{border-left-color:#2563eb}.event.tool_call{border-left-color:#f59e0b}.event.tool_result{border-left-color:#10b981}.event small{color:#64748b}.event pre,.prompt{white-space:pre-wrap;word-break:break-word;margin:.5rem 0 0}.prompt{background:#f8fafc;padding:10px;border-radius:6px;max-height:180px;overflow:auto}table{border-collapse:collapse;width:100%;background:#fff}td,th{border-bottom:1px solid #e2e8f0;padding:8px;text-align:left;font-size:13px}select,textarea{padding:8px;border:1px solid #cbd5e1;border-radius:6px;width:100%;margin:5px 0}textarea{min-height:100px}.row{display:flex;gap:10px}.row>*{flex:1}.ok{color:#047857}.warn{color:#b45309}@media(max-width:760px){.layout{grid-template-columns:1fr}nav{display:flex;overflow:auto;gap:4px}nav button{width:auto;white-space:nowrap}.content{padding:14px}}
</style><header><b>AgentTraceKit</b><span class='muted'>Review workspace · local only</span><span style='margin-left:auto'></span><button onclick="location.href='/'">← Sessions</button><button id='verifyBtn' onclick='verifyNow()'>Verify bundle</button><span id='status'></span></header><div class='layout'><nav><button onclick="tab('overview')">Overview</button><button onclick="tab('timeline')">Timeline</button><button onclick="tab('evidence')">Evidence</button><button onclick="tab('annotation')">Annotation</button><button onclick="tab('files')">Files</button></nav><main class='content'><section id='overview' class='tab active'></section><section id='timeline' class='tab'></section><section id='evidence' class='tab'></section><section id='annotation' class='tab'></section><section id='files' class='tab'></section></main></div><script>const DATA="""+data+""";const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const text=d=>d?.text||d?.content||d?.message||d?.command||JSON.stringify(d||{});function tab(id){document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));document.getElementById(id).classList.add('active');if(id==='timeline')drawTimeline();if(id==='evidence')drawEvidence();if(id==='annotation')drawAnnotation()};function drawOverview(){let m=DATA.manifest,v=DATA.validation;overview.innerHTML=`<div class='hero'><h1>${esc(m.project||'Codex session')}</h1><div class='muted'>${esc(m.provider)} · ${esc(m.timestamp||'Unknown time')} · ${esc(m.cwd||'Unknown folder')}</div><div class='stats'><div class='stat'><b>${m.interaction_count}</b><span class='muted'>interactions</span></div><div class='stat'><b>${m.event_count}</b><span class='muted'>events</span></div><div class='stat'><b>${m.parser_warning_count}</b><span class='muted'>parser warnings</span></div></div><p class='ok'>✓ Raw bytes preserved and SHA-256 recorded</p><p>Bundle integrity: <b>${esc(v.bundle_integrity)}</b><br>Trajectory parse: <b>${esc(v.trajectory_parse_status)}</b><br>Task success: <b>${esc(v.task_success)}</b></p><h3>First prompt</h3><div class='prompt'>${esc(DATA.interactions[0]?.user_message||'(No user message)')}</div></div>`};function drawTimeline(){timeline.innerHTML='<div class="card"><h2>Timeline</h2><input id="filter" placeholder="Search events…" oninput="drawTimeline()"></div>'+DATA.events.filter(e=>!document.getElementById('filter')?.value||JSON.stringify(e).toLowerCase().includes(document.getElementById('filter').value.toLowerCase())).map(e=>`<div class='event ${esc(e.event_type)}'><small>#${e.source_line} · ${esc(e.timestamp||'')} · ${esc(e.event_type)} · ${esc(e.event_id)}</small><pre>${esc(text(e.data))}</pre></div>`).join('')};function drawEvidence(){evidence.innerHTML='<div class="card"><h2>Evidence</h2><p class="muted">Every row points back to a physical raw source line.</p></div><table><tr><th>Event</th><th>Type</th><th>Source line</th><th>Raw type</th></tr>'+DATA.evidence.map(e=>`<tr><td>${esc(e.event_id)}</td><td>${esc(e.event_type)}</td><td>${esc(e.source_line)}</td><td>${esc(e.raw_type)}</td></tr>`).join('')+'</table>'};function drawAnnotation(){let labels=DATA.ontology.labels||[];annotation.innerHTML=`<div class='card'><h2>Annotation</h2><p class='muted'>Save reviewer labels without modifying raw evidence.</p><label>Interaction<select id='interaction'>${DATA.interactions.map(i=>`<option value='${esc(i.interaction_id)}'>${esc(i.interaction_id)} · ${esc(i.user_message).slice(0,80)}</option>`).join('')}</select></label><label>Label<select id='label'>${labels.map(l=>`<option>${esc(l.name)}</option>`).join('')}</select></label><label>Status<select id='review_status'><option>pending</option><option>accepted</option><option>rejected</option><option>needs_adjudication</option></select></label><label>Notes<textarea id='notes' placeholder='Reviewer notes'></textarea></label><button class='primary' onclick='saveAnnotation()'>Save annotation</button><span id='annStatus' class='status'></span></div><div class='card'><h3>Saved annotations (${DATA.annotations.length})</h3>${DATA.annotations.map(a=>`<p><b>${esc(a.interaction_id)}</b> · ${esc(a.label)} · ${esc(a.review_status)}<br><span class='muted'>${esc(a.notes||'')}</span></p>`).join('')||'<span class="muted">No annotations yet.</span>'}</div>`};async function saveAnnotation(){let a={interaction_id:document.getElementById('interaction').value,label:document.getElementById('label').value,review_status:document.getElementById('review_status').value,notes:document.getElementById('notes').value,event_ids:(DATA.interactions.find(i=>i.interaction_id===document.getElementById('interaction').value)||{}).event_ids||[]};let r=await fetch('/api/annotate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bundle:DATA.manifest._bundle_path,annotation:a})});let x=await r.json();annStatus.textContent=r.ok?'✓ Saved':'✗ '+x.error;if(r.ok){DATA.annotations.push(x.annotation);drawAnnotation()}}async function verifyNow(){status.textContent='Verifying…';let r=await fetch('/api/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bundle:DATA.manifest._bundle_path})});let x=await r.json();status.textContent=r.ok?'✓ '+x.bundle_integrity:'✗ '+x.error;setTimeout(()=>location.reload(),500)}function drawFiles(){files.innerHTML='<div class="card"><h2>Files</h2>'+Object.keys(DATA.manifest.files||{}).map(k=>`<p>${esc(k)}</p>`).join('')+'</div>'};DATA.manifest._bundle_path="""+json.dumps(str(bundle.resolve()),ensure_ascii=False)+""";drawOverview();drawFiles();</script>"""

class _Handler(BaseHTTPRequestHandler):
    adapter=CodexAdapter(); records=[]; output_root=Path.cwd()/'AgentTraceKit-output'
    def log_message(self,*args): pass
    def _send(self,body,status=200,ctype='application/json'):
        data=body.encode('utf-8') if isinstance(body,str) else body; self.send_response(status); self.send_header('Content-Type',ctype+'; charset=utf-8'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        u=urlparse(self.path)
        if u.path=='/': return self._send(PAGE,ctype='text/html')
        if u.path=='/api/sessions': return self._send(json.dumps(self.records,ensure_ascii=False))
        if u.path in ('/open','/report'):
            p=Path(parse_qs(u.query).get('path',[''])[0]); report=p/'timeline.html'
            if not report.exists() or not report.resolve().is_relative_to(self.output_root.resolve()): return self._send(json.dumps({'error':'report not found'}),404)
            if u.path=='/report': return self._send(dashboard_page(p),ctype='text/html')
            return self._send(json.dumps({'redirect':'/report?path='+quote(str(report.parent))}))
        self._send('Not found',404,'text/plain')
    def do_POST(self):
        endpoint=urlparse(self.path).path
        if endpoint not in ('/api/collect','/api/verify','/api/annotate'): return self._send('Not found',404,'text/plain')
        try:
            n=int(self.headers.get('Content-Length','0')); payload=json.loads(self.rfile.read(n)); wanted=None
            if endpoint=='/api/verify':
                wanted=Path(payload['bundle']).resolve()
                if not wanted.is_relative_to(self.output_root.resolve()): raise ValueError('Bundle is outside the local output directory')
                return self._send(json.dumps(verify_bundle(wanted),ensure_ascii=False))
            if endpoint=='/api/annotate':
                wanted=Path(payload['bundle']).resolve()
                if not wanted.is_relative_to(self.output_root.resolve()): raise ValueError('Bundle is outside the local output directory')
                _, apath=ensure_annotation_files(wanted); ann=dict(payload.get('annotation') or {})
                if not ann.get('interaction_id') or not ann.get('label'): raise ValueError('Interaction and label are required')
                from .annotation import make_annotation
                valid_ids={json.loads(x).get('interaction_id') for x in (wanted/'trajectory/interactions.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()}
                if ann['interaction_id'] not in valid_ids: raise ValueError('Interaction was not found in this bundle')
                record=make_annotation(wanted.name,ann['interaction_id'],ann['label'],ann.get('event_ids',[]),notes=ann.get('notes',''),status=ann.get('review_status','pending'))
                with apath.open('a',encoding='utf-8') as f: f.write(json.dumps(record,ensure_ascii=False)+'\n')
                refresh_manifest(wanted); return self._send(json.dumps({'annotation':record},ensure_ascii=False))
            wanted=Path(payload['path']).resolve()
            allowed={Path(r['path']).resolve() for r in self.records}
            if wanted not in allowed: raise ValueError('That session is not in the discovery list')
            parsed=self.adapter.parse_session(wanted); out=create_bundle(wanted,parsed,self.output_root); render_timeline(out); render_annotation_workbench(out); refresh_manifest(out)
            return self._send(json.dumps({'bundle':str(out.resolve()),'report_url':'/report?path='+quote(str(out.resolve()))}))
        except Exception as e: return self._send(json.dumps({'error':str(e)}),400)

def serve(output_root=None, port=0, open_browser=True):
    _Handler.adapter=CodexAdapter(); _Handler.records=session_records(_Handler.adapter); _Handler.output_root=Path(output_root or Path.cwd()/'AgentTraceKit-output'); server=ThreadingHTTPServer(('127.0.0.1',port),_Handler); url=f'http://127.0.0.1:{server.server_port}/';
    if open_browser: webbrowser.open(url)
    print(f'AgentTraceKit session browser: {url}\nClose this terminal or press Ctrl+C to stop.')
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()



