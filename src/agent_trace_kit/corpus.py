import json, sqlite3
from pathlib import Path
from .bundle import sha256

def index_corpus(root: Path):
    root=Path(root); db=root/'index.sqlite'; db.parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(db); con.executescript('''CREATE TABLE IF NOT EXISTS bundles(path TEXT PRIMARY KEY,bundle_id TEXT,provider TEXT,project TEXT,session_id TEXT,interactions INTEGER,events INTEGER); CREATE TABLE IF NOT EXISTS interactions(bundle_path TEXT,interaction_id TEXT,user_message TEXT,tool_calls INTEGER,reviewed INTEGER DEFAULT 0,PRIMARY KEY(bundle_path,interaction_id)); CREATE VIRTUAL TABLE IF NOT EXISTS interaction_search USING fts5(bundle_path,interaction_id,user_message);''')
    for mpath in root.rglob('manifest.json'):
        try: m=json.loads(mpath.read_text(encoding='utf-8')); bundle=mpath.parent
        except Exception: continue
        bid=m.get('raw_copy',{}).get('sha256','')
        con.execute('INSERT OR REPLACE INTO bundles VALUES(?,?,?,?,?,?,?)',(str(bundle),bid,m.get('provider'),m.get('project'),m.get('session_id'),m.get('interaction_count',0),m.get('event_count',0)))
        ip=bundle/'trajectory/interactions.jsonl'
        if ip.exists():
            for line in ip.read_text(encoding='utf-8').splitlines():
                try: i=json.loads(line)
                except Exception: continue
                con.execute('INSERT OR REPLACE INTO interactions VALUES(?,?,?,?,0)',(str(bundle),i.get('interaction_id'),i.get('user_message',''),len(i.get('tool_calls',[]))))
                con.execute('INSERT INTO interaction_search VALUES(?,?,?)',(str(bundle),i.get('interaction_id'),i.get('user_message','')))
    con.commit(); count=con.execute('SELECT COUNT(*) FROM bundles').fetchone()[0]; con.close(); return db,count
