import json, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote
from .adapters.codex import CodexAdapter
from .bundle import create_bundle, refresh_manifest
from .html import render_timeline, render_annotation_workbench

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
            if u.path=='/report': return self._send(report.read_text(encoding='utf-8'),ctype='text/html')
            return self._send(json.dumps({'redirect':'/report?path='+quote(str(report.parent))}))
        self._send('Not found',404,'text/plain')
    def do_POST(self):
        if urlparse(self.path).path!='/api/collect': return self._send('Not found',404,'text/plain')
        try:
            n=int(self.headers.get('Content-Length','0')); payload=json.loads(self.rfile.read(n)); wanted=Path(payload['path']).resolve()
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

