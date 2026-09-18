"""Small local browser UI for the AB execution and publishing workflow."""
from __future__ import annotations
import json, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from .github_upload import upload_bundle
from .pair_runner import PairSpec, export_dataset, run_pair, validate_spec

PAGE = """<!doctype html><meta charset='utf-8'><title>AgentTraceKit AB runner</title>
<style>body{font:15px system-ui;max-width:1100px;margin:24px auto;padding:0 18px;background:#f6f7f9;color:#1f2937}.card{background:white;border:1px solid #d8dee8;border-radius:10px;padding:18px;margin:14px 0}label{display:block;margin:9px 0;font-weight:600}input,textarea,select{display:block;width:100%;padding:8px;border:1px solid #b8c2d1;border-radius:6px;font:inherit;margin-top:4px}textarea{min-height:110px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.buttons{display:flex;gap:9px;flex-wrap:wrap}button{padding:9px 14px;border:0;border-radius:6px;background:#2563eb;color:white;cursor:pointer}button.secondary{background:#475569}pre{white-space:pre-wrap;background:#0f172a;color:#e2e8f0;padding:12px;border-radius:6px;max-height:330px;overflow:auto}@media(max-width:760px){.grid{grid-template-columns:1fr}}</style>
<h1>AB 评测与上传</h1><p>填写两个本地目录，统一提示词和检查命令；先检查，再运行，最后生成 CSV 并推送到已配置凭据的 Git 仓库。</p>
<div class='card'><label>统一提示词<textarea id='prompt' placeholder='粘贴任务提示词'></textarea></label><div class='grid'><label>A 目录<input id='a_directory'></label><label>B 目录<input id='b_directory'></label><label>运行环境<input id='environment' placeholder='go version go1.26.0 linux/amd64'></label><label>评测人<input id='evaluator'></label><label>生成模式<input id='generation_mode' value='0-1代码生成'></label><label>Provider<input id='provider' value='Claude Code'></label><label>难度<input id='difficulty' value='困难'></label><label>操作系统<input id='os_name' value='Windows'></label><label>依赖说明<input id='dependencies' value='无外部依赖'></label><label>基线提交<input id='baseline_commit'></label><label>结论<select id='winner'><option value=''>请选择</option><option>A 更好</option><option>Same</option><option>B 更好</option></select></label><label>标签<input id='label' placeholder='有效'></label></div><label>结论理由<textarea id='rationale'></textarea></label><label>检查命令（每行一条）<textarea id='commands'>go version
go build ./...
go vet ./...
go test ./...</textarea></label></div>
<div class='card'><div class='grid'><label>A 会话 ID<input id='a_session_id'></label><label>B 会话 ID<input id='b_session_id'></label><label>A 轨迹 URL<input id='a_trace_url'></label><label>B 轨迹 URL<input id='b_trace_url'></label><label>A 视频 URL<input id='a_video_url'></label><label>B 视频 URL<input id='b_video_url'></label><label>上传到 Git 仓库目录<input id='repo'></label><label>上传子目录<input id='destination' value='data/pairs'></label></div><div class='buttons'><button onclick="api('/api/validate')">检查缺失项</button><button onclick="api('/api/run')">运行 A/B 检查</button><button class='secondary' onclick="api('/api/export')">生成数据 CSV</button><button class='secondary' onclick="api('/api/upload')">生成并上传</button></div></div><div class='card'><h2>结果</h2><pre id='out'>等待操作…</pre></div>
<script>const ids=['prompt','a_directory','b_directory','environment','evaluator','generation_mode','provider','difficulty','os_name','dependencies','baseline_commit','winner','label','rationale','commands','a_session_id','b_session_id','a_trace_url','b_trace_url','a_video_url','b_video_url','repo','destination'];function payload(){let x={};ids.forEach(id=>x[id]=document.getElementById(id).value);return x}async function api(path){out.textContent='处理中…';try{let r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload())});let x=await r.json();out.textContent=JSON.stringify(x,null,2)}catch(e){out.textContent=String(e)}}</script>"""

class _Handler(BaseHTTPRequestHandler):
    output = Path.cwd() / "AgentTraceKit-output" / "pair-ui"
    def log_message(self, *_): pass
    def send_json(self, value, status=200):
        data=json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8'); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if urlparse(self.path).path == '/':
            data=PAGE.encode('utf-8'); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
        self.send_json({'error':'not found'},404)
    def do_POST(self):
        try:
            n=int(self.headers.get('Content-Length','0')); raw=json.loads(self.rfile.read(n)); spec=PairSpec.from_mapping(raw); endpoint=urlparse(self.path).path; checks=validate_spec(spec)
            if endpoint == '/api/validate': return self.send_json({'valid':all(x['ok'] for x in checks),'checks':checks})
            if endpoint not in ('/api/run','/api/export','/api/upload'): return self.send_json({'error':'not found'},404)
            if endpoint != '/api/run':
                decision_checks=validate_spec(spec, require_decision=True)
                if not all(x['ok'] for x in decision_checks): return self.send_json({'ok':False,'error':'导出前请补齐结论字段','checks':decision_checks},400)
            _Handler.output.mkdir(parents=True,exist_ok=True); result=run_pair(spec,_Handler.output)
            if endpoint == '/api/run': return self.send_json(result)
            csv_path=export_dataset(spec,result,_Handler.output/'pair-dataset.csv')
            if endpoint == '/api/export': return self.send_json({'ok':True,'csv':str(csv_path),'run':result})
            if not raw.get('repo'): raise ValueError('上传前请填写目标 Git 仓库目录')
            uploaded=upload_bundle(raw['repo'],_Handler.output,raw.get('destination') or 'data/pairs',push=True); return self.send_json({'ok':True,'csv':str(csv_path),'upload':uploaded})
        except Exception as exc: return self.send_json({'ok':False,'error':str(exc)},400)

def serve_pair_ui(port=0, open_browser=True, output=None):
    _Handler.output=Path(output or Path.cwd()/'AgentTraceKit-output'/'pair-ui').resolve(); server=ThreadingHTTPServer(('127.0.0.1',port),_Handler); url=f'http://127.0.0.1:{server.server_port}/'
    if open_browser: webbrowser.open(url)
    print(f'AgentTraceKit AB runner: {url}\nClose this terminal or press Ctrl+C to stop.')
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

