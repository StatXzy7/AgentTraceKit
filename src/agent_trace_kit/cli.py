import argparse, os, sys, traceback, webbrowser
from pathlib import Path
from .adapters.codex import CodexAdapter, codex_home
from .bundle import create_bundle, refresh_manifest
from .html import render_timeline, render_annotation_workbench
from .annotation import ensure_annotation_files
from .corpus import index_corpus
from .verify import verify_bundle
VERSION='0.2.0'
try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception: pass
def _out_root(): return Path(os.environ.get('AGENT_TRACE_KIT_OUTPUT',Path.cwd()/'AgentTraceKit-output'))
def doctor():
 a=CodexAdapter(); home=codex_home(); sessions=a.discover_sessions(); print(f'AgentTraceKit {VERSION}\nOS: {sys.platform}')
 if a.detect(): print('✓ Codex CLI detected'); print(f'  Version: {a.version() or "unknown"}')
 else: print('✗ Codex CLI was not found.\n  Existing JSONL can still be collected with: atk collect --input <file>')
 print(f'Codex home: {home}\nSession directory: {home/"sessions"}\nSessions found: {len(sessions)}')
 try: _out_root().mkdir(parents=True,exist_ok=True); print(f'✓ Output directory writable: {_out_root()}')
 except OSError as e: print(f'✗ Cannot write output directory: {e}')
 print('✓ Default browser check complete')
def choose(a):
 ss=a.discover_sessions()
 if not ss: print('Could not find a Codex session.\n\nI checked:\n  '+str(codex_home()/'sessions')+'\n\nTry running Codex once, or use atk collect --input FILE.'); return None
 print('Recent Codex sessions:')
 for n,p in enumerate(ss[:20],1):
  i=a.inspect_session(p); print(f'\n{n}. {Path(i.get("cwd") or "unknown").name}\n   {i.get("timestamp") or i.get("mtime")}\n   "{i.get("preview") or "(no user message preview)"}"\n   id: {str(i.get("session_id"))[:12]}')
 while True:
  try: n=int(input('\nChoose a session number: '))
  except (ValueError,EOFError): print('Please enter a number.'); continue
  if 1<=n<=min(20,len(ss)): return ss[n-1]
  print('Please choose one of the listed numbers.')
def collect(args):
 a=CodexAdapter(); discovered=a.discover_sessions(); source=Path(args.input).expanduser() if args.input else (discovered[0] if args.latest and discovered else choose(a))
 if not source: return 1
 if not source.exists(): print(f'✗ Input file not found: {source}'); return 1
 p=a.parse_session(source); out=create_bundle(source,p,Path(args.output or _out_root())); render_timeline(out); render_annotation_workbench(out); refresh_manifest(out)
 i=a.inspect_session(source); print(f'✓ Collected\n  Provider: codex\n  Project: {Path(i.get("cwd") or "unknown").name}\n  Session id: {p.session_id}\n  Source: {source}\n  Timestamp: {p.timestamp or i.get("mtime")}\n  Bundle: {out}\n  Report: {out/"timeline.html"}')
 return 0
def verify_cmd(path):
 if not path: path=input('Bundle path: ').strip()
 target=Path(path).expanduser()
 if not target.exists():
  print(f'✗ Bundle directory was not found: {target}')
  print(f'  List available bundles with: Get-ChildItem "{_out_root()}" -Directory')
  return 1
 r=verify_bundle(target); print(('✓' if r.get('bundle_integrity')=='ok' else '✗')+f' bundle_integrity: {r.get("bundle_integrity","unknown")}'); print(f'trajectory_parse_status: {r.get("trajectory_parse_status","unknown")}\nagent_run_status: {r.get("agent_run_status","unknown")}\ntask_success: {r.get("task_success","unknown")}')
 for x in r.get('issues',[]): print('  ✗ '+x)
 return 0 if r['bundle_integrity']=='ok' else 1
def guided():
 print('AgentTraceKit\n\nWhat would you like to do?\n[1] Collect my latest Codex session\n[2] Choose a Codex session\n[3] Record a new Codex task\n[4] Verify a trajectory bundle\n[5] Help')
 try: n=input('Choose [1-5]: ').strip()
 except EOFError: return 0
 if n=='1': return collect(argparse.Namespace(input=None,latest=True,output=None))
 if n=='2': return collect(argparse.Namespace(input=None,latest=False,output=None))
 if n=='4': return verify_cmd(None)
 print('Use atk collect, atk doctor, atk verify PATH, or atk open [PATH].'); return 0
def main(argv=None):
 ap=argparse.ArgumentParser(prog='atk'); sub=ap.add_subparsers(dest='cmd'); sub.add_parser('doctor'); c=sub.add_parser('collect'); c.add_argument('--latest',action='store_true'); c.add_argument('--input'); c.add_argument('--output'); v=sub.add_parser('verify'); v.add_argument('path'); o=sub.add_parser('open'); o.add_argument('path',nargs='?'); w=sub.add_parser('view'); w.add_argument('path'); an=sub.add_parser('annotate'); an.add_argument('path'); co=sub.add_parser('corpus'); co.add_argument('action',choices=['index']); co.add_argument('path'); sub.add_parser('run'); args=ap.parse_args(argv)
 try:
  if not args.cmd:return guided()
  if args.cmd=='doctor':doctor();return 0
  if args.cmd=='collect':return collect(args)
  if args.cmd=='verify':return verify_cmd(args.path)
  if args.cmd=='open':
   p=Path(args.path) if args.path else max(_out_root().glob('*/timeline.html'),key=lambda x:x.stat().st_mtime,default=None)
   if not p: print('No bundle found. Run atk collect first.'); return 1
   webbrowser.open(p.resolve().as_uri()); print(f'✓ Opened {p}'); return 0
  if args.cmd=='view':
   p=Path(args.path); report=p/'timeline.html' if p.is_dir() else p
   if not report.exists():
    print(f'✗ Timeline not found: {report}')
    print(f'  List available bundles with: Get-ChildItem "{_out_root()}" -Directory')
    return 1
   webbrowser.open(report.resolve().as_uri()); print(f'✓ Opened {report}'); return 0
  if args.cmd=='annotate':
   p=Path(args.path); ensure_annotation_files(p); work=render_annotation_workbench(p); webbrowser.open(work.resolve().as_uri()); print(f'✓ Annotation workbench: {work}'); return 0
  if args.cmd=='corpus':
   if args.action=='index': db,n=index_corpus(Path(args.path)); print(f'✓ Indexed {n} bundles\n  SQLite index: {db}'); return 0
  if args.cmd=='run': print('atk run is reserved for a future safe Codex non-interactive adapter in v0.1.'); return 0
 except Exception as e:
  if '--debug' in (argv or sys.argv): traceback.print_exc()
  else: print(f'✗ {e}')
  return 1
if __name__=='__main__': raise SystemExit(main())


