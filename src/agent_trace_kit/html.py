import html, json
from pathlib import Path
def render_timeline(bundle: Path):
    events=[json.loads(x) for x in (bundle/'trajectory/events.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()]
    rows=[]
    for e in events:
        text=e.get('data',{}).get('text') or e.get('data',{}).get('content') or e.get('data',{}).get('message') or e.get('data',{}).get('command') or json.dumps(e.get('data',{}),ensure_ascii=False)
        rows.append(f"<article data-type='{html.escape(e['event_type'])}'><small>#{e['source_line']} · {html.escape(e['timestamp'] or '')} · {html.escape(e['event_type'])}</small><pre>{html.escape(str(text))}</pre></article>")
    doc="""<!doctype html><meta charset='utf-8'><title>AgentTraceKit timeline</title><style>body{font:15px system-ui;max-width:1000px;margin:2rem auto;padding:0 1rem;background:#f7f7f8;color:#222}header{position:sticky;top:0;background:#f7f7f8;padding:1rem 0}input{width:100%;padding:.7rem;border:1px solid #bbb;border-radius:6px}article{background:white;border:1px solid #ddd;border-radius:8px;padding:.7rem;margin:.6rem 0}small{color:#666}pre{white-space:pre-wrap;word-break:break-word;margin:.5rem 0 0}article[data-type=tool_call]{border-left:4px solid #e59f00}article[data-type=tool_result]{border-left:4px solid #4c9}article[data-type=user_message]{border-left:4px solid #58f}button{margin:.3rem;padding:.4rem}</style><header><h1>AgentTraceKit timeline</h1><input id='q' placeholder='Search this session…'><button onclick="document.querySelectorAll('article').forEach(a=>a.open=!a.open)">Toggle details</button></header><main>"""+''.join(rows)+"</main><script>q.oninput=()=>document.querySelectorAll('article').forEach(a=>a.hidden=!!q.value&&!a.innerText.toLowerCase().includes(q.value.toLowerCase()))</script>"
    (bundle/'timeline.html').write_text(doc,encoding='utf-8'); return bundle/'timeline.html'
