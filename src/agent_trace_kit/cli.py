import argparse, json, os, sys, traceback, webbrowser
from pathlib import Path
from .adapters.codex import CodexAdapter
from .adapters.claude import ClaudeAdapter
from .bundle import create_bundle, refresh_manifest
from .html import render_timeline, render_annotation_workbench
from .annotation import ensure_annotation_files
from .corpus import index_corpus
from .browse import serve
from .verify import verify_bundle
from .config import DEFAULTS, load_config, redacted, write_toml
from .pairwise import create_pair, load_pair, bind_side, import_review, export_pair
from .pair_ui import serve_pair_ui
VERSION='0.4.0'
try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception: pass
def _out_root(): return Path(os.environ.get('AGENT_TRACE_KIT_OUTPUT',Path.cwd()/'AgentTraceKit-output'))
def _adapter(provider): return ClaudeAdapter() if provider=='claude' else CodexAdapter()
def doctor():
 print(f'AgentTraceKit {VERSION}\nOS: {sys.platform}')
 for provider in ('codex','claude'):
  a=_adapter(provider); print(f'{"✓" if a.detect() else "✗"} {provider} CLI: {a.version() or "not detected"}'); print(f'  sessions: {len(a.discover_sessions())}')
 _out_root().mkdir(parents=True,exist_ok=True); print(f'✓ Output directory writable: {_out_root()}')
def collect(args):
 a=_adapter(args.provider); discovered=a.discover_sessions(); source=Path(args.input).expanduser() if args.input else (discovered[0] if args.latest and discovered else None)
 if not source: print('No input session. Use --input FILE or --latest.'); return 1
 if not source.exists(): print(f'✗ Input file not found: {source}'); return 1
 p=a.parse_session(source); out=create_bundle(source,p,Path(args.output or _out_root())); render_timeline(out); render_annotation_workbench(out); refresh_manifest(out); print(f'✓ Collected provider={p.provider} session={p.session_id}\n  Bundle: {out}'); return 0
def config_cmd(args):
 path=Path(args.path or 'config.toml')
 if args.action=='init': write_toml(path,DEFAULTS); print(f'✓ wrote {path}'); return 0
 cfg,sources=load_config([path] if path.exists() else [])
 if args.action=='validate': print(json.dumps({'valid':True,'config':cfg},ensure_ascii=False,indent=2)); return 0
 if args.action=='show': print(json.dumps(redacted(cfg),ensure_ascii=False,indent=2)); return 0
 if args.action=='explain':
  if args.key not in cfg: print(f'unknown key: {args.key}'); return 1
  print(json.dumps({'key':args.key,'value':redacted({args.key:cfg[args.key]})[args.key],'source':sources.get(args.key),'precedence':'defaults < config < CLI'},ensure_ascii=False,indent=2)); return 0
def pair_cmd(args):
 root=Path(args.root or (_out_root()/'pairs'))
 if args.action=='create': print(create_pair(root,args.task,args.prompt,args.provider,args.difficulty)); return 0
 p=Path(args.path)
 if args.action=='bind': bind_side(p,args.side,args.session_id,args.bundle); print(f'✓ bound {args.side}'); return 0
 if args.action=='review-import': import_review(p,args.review); print('✓ review imported'); return 0
 if args.action=='export':
  result=export_pair(p,args.output or str(p/'export.csv'),args.strict); print(json.dumps(result,ensure_ascii=False)); return 1 if args.strict and result['missing'] else 0
 if args.action=='status': print(json.dumps(load_pair(p),ensure_ascii=False,indent=2)); return 0
 if args.action=='validate':
  d=load_pair(p); missing=[x for x in ('A','B') if not d['sides'].get(x)]; print(json.dumps({'valid':not missing,'missing':missing},ensure_ascii=False)); return 1 if missing else 0
 if args.action in ('prepare','watch','check','resume','snapshot','record','attach-video','publish','run'):
  if args.action=='run' and args.dry_run: print(json.dumps({'dry_run':True,'provider':load_pair(p)['provider']})); return 0
  print(f'{args.action}: supported state operation requires explicit local inputs'); return 0
def main(argv=None):
 ap=argparse.ArgumentParser(prog='atk'); sub=ap.add_subparsers(dest='cmd'); sub.add_parser('doctor'); c=sub.add_parser('collect'); c.add_argument('--provider',choices=['codex','claude'],default='codex'); c.add_argument('--latest',action='store_true'); c.add_argument('--input'); c.add_argument('--output'); v=sub.add_parser('verify'); v.add_argument('path'); op=sub.add_parser('open'); op.add_argument('path',nargs='?'); w=sub.add_parser('view'); w.add_argument('path'); an=sub.add_parser('annotate'); an.add_argument('path'); br=sub.add_parser('browse'); br.add_argument('--port',type=int,default=0); br.add_argument('--no-browser',action='store_true'); pu=sub.add_parser('pair-ui',help='AB目录检查、导出与Git上传'); pu.add_argument('--port',type=int,default=0); pu.add_argument('--no-browser',action='store_true'); pu.add_argument('--output'); co=sub.add_parser('corpus'); co.add_argument('action',choices=['index']); co.add_argument('path'); cf=sub.add_parser('config'); cf.add_argument('action',choices=['init','validate','show','explain']); cf.add_argument('key',nargs='?'); cf.add_argument('--path'); cf.add_argument('--redacted',action='store_true'); pa=sub.add_parser('pair'); pa.add_argument('action',choices=['create','prepare','watch','bind','collect','snapshot','check','record','attach-video','review-import','validate','export','publish','status','resume','run']); pa.add_argument('path',nargs='?'); pa.add_argument('--root'); pa.add_argument('--task',default=''); pa.add_argument('--prompt',default=''); pa.add_argument('--provider',choices=['codex','claude'],default='codex'); pa.add_argument('--difficulty',default=''); pa.add_argument('--side'); pa.add_argument('--session-id'); pa.add_argument('--bundle'); pa.add_argument('--review'); pa.add_argument('--output'); pa.add_argument('--strict',action='store_true'); pa.add_argument('--dry-run',action='store_true'); dm=sub.add_parser('demo'); dm.add_argument('--synthetic',action='store_true'); args=ap.parse_args(argv)
 try:
  if args.cmd=='doctor': doctor(); return 0
  if args.cmd=='collect': return collect(args)
  if args.cmd=='config': return config_cmd(args)
  if args.cmd=='pair': return pair_cmd(args)
  if args.cmd=='verify': r=verify_bundle(Path(args.path)); print(json.dumps(r,ensure_ascii=False,indent=2)); return 0 if r['bundle_integrity']=='ok' else 1
  if args.cmd=='open':
   p=Path(args.path) if args.path else max(_out_root().glob('*/timeline.html'),key=lambda x:x.stat().st_mtime,default=None); webbrowser.open(p.resolve().as_uri()) if p else None; return 0 if p else 1
  if args.cmd=='view': p=Path(args.path); webbrowser.open((p/'timeline.html' if p.is_dir() else p).resolve().as_uri()); return 0
  if args.cmd=='annotate': p=Path(args.path); ensure_annotation_files(p); webbrowser.open(render_annotation_workbench(p).resolve().as_uri()); return 0
  if args.cmd=='browse': serve(_out_root(),args.port,not args.no_browser); return 0
  if args.cmd=='pair-ui': serve_pair_ui(args.port,not args.no_browser,args.output); return 0
  if args.cmd=='corpus': db,n=index_corpus(Path(args.path)); print(f'✓ Indexed {n} bundles\n  SQLite index: {db}'); return 0
  if args.cmd=='demo':
   import tempfile
   from datetime import datetime, timezone
   demo=Path(tempfile.mkdtemp(prefix='atk-demo-')); raw=[]
   for sid in ('synthetic-a','synthetic-b'):
    src=demo/(sid+'.jsonl'); rows=[{'type':'session_meta','payload':{'session_id':sid,'cwd':str(demo),'cli_version':'synthetic'},'timestamp':datetime.now(timezone.utc).isoformat()},{'type':'event_msg','payload':{'type':'user_message','message':'synthetic prompt'}},{'type':'response_item','payload':{'item':{'type':'message','role':'assistant','content':'synthetic result'}}},{'type':'event_msg','payload':{'type':'task_complete'}}]; src.write_text('\n'.join(json.dumps(x) for x in rows)+'\n',encoding='utf-8'); raw.append(src)
   from .pairwise import create_pair, bind_side
   pair=create_pair(demo,'synthetic','synthetic prompt','codex','困难')
   (demo/'bundles').mkdir()
   for side,src in zip(('A','B'),raw):
    parsed=CodexAdapter().parse_session(src); bundle=create_bundle(src,parsed,demo/'bundles'); render_timeline(bundle); render_annotation_workbench(bundle); refresh_manifest(bundle); bind_side(pair,side,parsed.session_id,bundle)
   result=export_pair(pair,demo/'pair.csv'); print(json.dumps({'pair':str(pair),'export':result,'evidence_root':str(demo)},ensure_ascii=False,indent=2)); return 0
  return 0
 except Exception as e:
  if '--debug' in (argv or sys.argv): traceback.print_exc()
  else: print(f'✗ {e}')
  return 1
if __name__=='__main__': raise SystemExit(main())

