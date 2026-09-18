from __future__ import annotations
import hashlib, json, os, shutil, subprocess
from pathlib import Path
from datetime import datetime
from ..models import NormalizedEvent, ParseWarning, ParsedSession
from .base import AgentAdapter

class ClaudeAdapter(AgentAdapter):
    name = "claude"
    def roots(self):
        home = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home()/".claude")).expanduser()
        return [home/"projects", home/"sessions", home]
    def detect(self): return shutil.which("claude") is not None
    def version(self):
        try: return subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=5).stdout.strip() or None
        except Exception: return None
    def discover_sessions(self):
        files=[]
        for root in self.roots():
            if root.exists(): files.extend(root.rglob("*.jsonl"))
        return sorted(set(files), key=lambda p: p.stat().st_mtime, reverse=True)
    def inspect_session(self, path):
        info={"path":str(path),"mtime":datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="minutes"),"cwd":None,"session_id":None,"timestamp":None,"preview":None}
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try: rec=json.loads(line)
                except Exception: continue
                info["session_id"] = info["session_id"] or rec.get("sessionId") or rec.get("session_id")
                info["cwd"] = info["cwd"] or rec.get("cwd")
                info["timestamp"] = info["timestamp"] or rec.get("timestamp")
                if info["preview"] is None and rec.get("type") in ("user", "human"):
                    info["preview"] = str(rec.get("message", rec.get("content", "")))[:100]
        except OSError: pass
        info["session_id"] = info["session_id"] or path.stem
        return info
    def parse_session(self, path):
        events=[]; warnings=[]; counts={}; users=[]; sid=path.stem; cwd=ts=cli=None
        for no, raw in enumerate(path.open("r", encoding="utf-8", errors="replace"), 1):
            text=raw.rstrip("\r\n")
            if not text.strip(): continue
            try: rec=json.loads(text)
            except json.JSONDecodeError as e: warnings.append(ParseWarning(no,"malformed_json",e.msg,text)); continue
            if not isinstance(rec,dict): warnings.append(ParseWarning(no,"non_object","record is not an object",text)); continue
            typ=str(rec.get("type","unknown")); counts[typ]=counts.get(typ,0)+1; sid=rec.get("sessionId") or rec.get("session_id") or sid; cwd=rec.get("cwd") or cwd; ts=rec.get("timestamp") or ts
            if typ in ("user","human"):
                msg=rec.get("message",rec.get("content","")); msg=msg if isinstance(msg,str) else json.dumps(msg,ensure_ascii=False); et="user_message"; data={"text":msg,**rec}; users.append(msg)
            elif typ in ("assistant","assistant_message"): et="assistant_message"; data=rec
            elif typ in ("tool_result", "tool_use", "tool_call"): et="tool_result" if typ=="tool_result" else "tool_call"; data=rec
            elif typ in ("system","progress","stop","summary"): et="lifecycle"; data=rec
            else: et="unknown"; data=rec
            eid="e_"+hashlib.sha256(f"{no}:{text}".encode()).hexdigest()[:16]
            events.append(NormalizedEvent("1.1","claude",sid,eid,rec.get("timestamp"),et,str(path),no,typ,data))
        return ParsedSession("claude",sid,cwd,ts,cli,events,warnings,counts,users)
