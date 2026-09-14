import json, os, re, shutil, subprocess
from pathlib import Path
from datetime import datetime
from .base import AgentAdapter
from ..models import NormalizedEvent, ParseWarning, ParsedSession

def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home()/".codex")).expanduser()

def _text(value):
    if isinstance(value, str): return value
    if isinstance(value, list):
        return "".join(_text(x.get("text", x.get("content", "")) if isinstance(x, dict) else x) for x in value)
    return "" if value is None else str(value)

class CodexAdapter(AgentAdapter):
    name = "codex"
    def detect(self): return shutil.which("codex") is not None
    def version(self):
        try: return subprocess.run(["codex","--version"], capture_output=True, text=True, timeout=5).stdout.strip() or None
        except Exception: return None
    def discover_sessions(self):
        root = codex_home()/"sessions"
        if not root.exists(): return []
        return sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    def inspect_session(self, path):
        info={"path":str(path),"mtime":datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="minutes"),"cwd":None,"session_id":None,"timestamp":None,"preview":None}
        try:
            with path.open("r",encoding="utf-8-sig",errors="replace",newline="") as f:
                for line in f:
                    try: rec=json.loads(line)
                    except Exception: continue
                    typ=rec.get("type"); payload=rec.get("payload") or {}
                    if typ=="session_meta":
                        info["session_id"]=payload.get("id") or payload.get("session_id") or payload.get("thread_id")
                        info["cwd"]=payload.get("cwd"); info["timestamp"]=rec.get("timestamp") or payload.get("timestamp")
                    if info["preview"] is None:
                        txt=self._extract_user(rec)
                        if txt: info["preview"]=txt.replace("\n"," ")[:100]
        except OSError: pass
        if not info["session_id"]: info["session_id"]=path.stem.removeprefix("rollout-")
        return info
    def _extract_user(self, rec):
        typ=rec.get("type"); p=rec.get("payload") or {}
        if typ=="event_msg" and p.get("type") in ("user_message","user_message_event"): return _text(p.get("message") or p.get("text") or p.get("content"))
        if typ=="response_item":
            item=p.get("item") if isinstance(p.get("item"),dict) else p
            if item.get("type") in ("message","user_message") and item.get("role")=="user": return _text(item.get("content") or item.get("text"))
        return ""
    def parse_session(self, path):
        events=[]; warnings=[]; counts={}; users=[]; sid=path.stem; cwd=None; ts=None; cli=None; seen_user=set(); pending={}; idx=0
        with path.open("r",encoding="utf-8-sig",errors="replace",newline="") as f:
            for line_no, raw in enumerate(f,1):
                raw_no_nl=raw.rstrip("\r\n");
                if not raw_no_nl.strip(): continue
                try: rec=json.loads(raw_no_nl)
                except json.JSONDecodeError as e:
                    warnings.append(ParseWarning(line_no,"malformed_json",f"Invalid JSON: {e.msg}",raw_no_nl)); continue
                if not isinstance(rec,dict):
                    warnings.append(ParseWarning(line_no,"non_object", "JSON record is not an object", raw_no_nl)); continue
                typ=str(rec.get("type", "unknown")); counts[typ]=counts.get(typ,0)+1; payload=rec.get("payload") or {}; stamp=rec.get("timestamp")
                if typ=="session_meta":
                    sid=payload.get("id") or payload.get("session_id") or payload.get("thread_id") or sid; cwd=payload.get("cwd"); ts=stamp or payload.get("timestamp"); cli=payload.get("cli_version")
                    et="lifecycle"; data=dict(payload)
                elif typ=="turn_context": et="lifecycle"; data=dict(payload)
                elif typ=="event_msg":
                    mt=payload.get("type",""); textv=self._extract_user(rec)
                    if textv:
                        key=(textv, payload.get("id") or payload.get("message_id"));
                        if key in seen_user: continue
                        seen_user.add(key); users.append(textv); et="user_message"; data={"text":textv,**payload}
                    elif mt in ("task_complete","turn_complete","session_end","shutdown_complete"): et="lifecycle"; data=dict(payload)
                    elif "error" in mt or mt in ("task_failed","turn_failed"): et="lifecycle"; data=dict(payload)
                    else: et="unknown"; data=dict(payload)
                elif typ=="response_item":
                    item=payload.get("item") if isinstance(payload.get("item"),dict) else payload; it=str(item.get("type", "")); role=item.get("role")
                    if role=="user" or it=="user_message":
                        textv=_text(item.get("content") or item.get("text")); key=(textv,item.get("id"));
                        if key in seen_user: continue
                        seen_user.add(key); users.append(textv); et="user_message"; data=dict(item)
                    elif role=="assistant" or it in ("message","assistant_message"): et="assistant_message"; data=dict(item)
                    elif it in ("function_call","custom_tool_call","tool_call"): et="tool_call"; data=dict(item); pending[item.get("call_id") or item.get("id") or f"line-{line_no}"]=line_no
                    elif it in ("function_call_output","custom_tool_call_output","tool_result","tool_output"): et="tool_result"; data=dict(item)
                    elif it in ("reasoning","reasoning_summary"): et="assistant_message"; data={"reasoning_summary":item.get("summary") or item.get("text") or item}
                    else: et="unknown"; data=dict(item)
                else: et="unknown"; data=dict(payload) if isinstance(payload,dict) else {"payload":payload}
                idx+=1; events.append(NormalizedEvent("1.0","codex",sid,f"e{idx:06d}",stamp,et,str(path),line_no,typ,data))
        return ParsedSession("codex",sid,cwd,ts,cli,events,warnings,counts,users)
