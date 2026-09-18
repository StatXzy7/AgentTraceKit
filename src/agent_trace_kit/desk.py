"""Pair Desk — local browser console for the pairwise GSB workflow.

Run with:  python -m agent_trace_kit.desk  (or  atk desk)
"""
from __future__ import annotations

import json
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import oss as oss_mod
from . import workspace as ws
from .checklist import run_checklist
from .desk_store import CONCLUSIONS, DIFFICULTIES, TASK_TYPES, DeskStore
from .export_tsv import HEADERS, export_tsv, job_row, upload_side
from .runner import PairRunner

PAGE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pair 交付台 · AgentTraceKit</title>
<style>
:root{--bg:#f4f5f8;--card:#fff;--line:#dde2ea;--ink:#1f2937;--muted:#6b7280;--blue:#2563eb;--ok:#15803d;--bad:#b91c1c;--warn:#b45309;--chip:#eef2ff}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,"Segoe UI",sans-serif;background:var(--bg);color:var(--ink)}
header{background:#0f172a;color:#fff;padding:12px 20px;display:flex;align-items:center;gap:14px}
header h1{font-size:16px;margin:0}header .sp{flex:1}
.wrap{max-width:1280px;margin:18px auto;padding:0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:14px 0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
label{display:block;font-weight:600;margin:8px 0 3px;font-size:13px}
input,textarea,select{width:100%;padding:8px;border:1px solid #b8c2d1;border-radius:6px;font:inherit;background:#fff}
textarea{min-height:80px;resize:vertical}
button{padding:8px 13px;border:0;border-radius:6px;background:var(--blue);color:#fff;cursor:pointer;font:inherit}
button.sec{background:#475569}button.ghost{background:#e5e7eb;color:#111}button:disabled{opacity:.45;cursor:not-allowed}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{border-bottom:1px solid var(--line);padding:7px 8px;text-align:left;vertical-align:top}
th{background:#f8fafc;position:sticky;top:0}
.tag{display:inline-block;padding:1px 8px;border-radius:20px;font-size:12px;font-weight:600}
.t-draft{background:#e5e7eb}.t-ready{background:#dbeafe}.t-running{background:#fef3c7}.t-evidence_ready{background:#dcfce7}.t-failed{background:#fee2e2}.t-done{background:#bbf7d0}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px}.g{background:var(--ok)}.r{background:var(--bad)}.y{background:var(--warn)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.muted{color:var(--muted)}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
pre.log{background:#0f172a;color:#e2e8f0;padding:10px;border-radius:6px;max-height:260px;overflow:auto;white-space:pre-wrap;font-size:12px}
.toast{position:fixed;right:18px;bottom:18px;background:#111827;color:#fff;padding:10px 14px;border-radius:8px;display:none;max-width:420px;z-index:9}
a{color:var(--blue)}
.sidebox{border:1px solid var(--line);border-radius:8px;padding:12px;background:#fcfcfd}
.sidebox h3{margin:0 0 8px;font-size:14px}
.kv{font-size:12px;color:var(--muted);word-break:break-all}
.progress{height:6px;background:#e5e7eb;border-radius:4px;overflow:hidden}.progress>i{display:block;height:100%;background:var(--blue)}
</style></head><body>
<header><h1>Pair 交付台</h1><span class="muted" style="color:#94a3b8">同模型 · 同环境 · 同提示词 · A/B 双跑</span><span class="sp"></span>
<button class="ghost" onclick="openSettings()">⚙ 设置</button>
<button class="ghost" onclick="loadJobs()">🔄 刷新</button></header>
<div class="wrap">

<div class="card">
  <div class="btns" style="margin-top:0">
    <button onclick="showCreate()">＋ 新建任务</button>
    <button class="sec" onclick="showBatch()">📋 批量导入提示词</button>
    <span class="muted" style="align-self:center">并行队列自动运行；你只需要录产物视频 + 写 GSB。</span>
  </div>
</div>

<div id="createPanel" class="card" style="display:none">
  <h2 style="margin-top:0">新建一条 pair 任务</h2>
  <label>User Prompt（完整原文，A/B 共用）</label><textarea id="c_prompt" style="min-height:150px"></textarea>
  <div class="grid3">
    <label>任务类型<select id="c_task_type"></select></label>
    <label>难度<select id="c_difficulty"></select></label>
    <label>语言/框架<input id="c_stack" placeholder="Go, Gin / Python, FastAPI"></label>
    <label>环境可复现等级<input id="c_repro" value="无外部依赖"></label>
    <label>运行环境说明<input id="c_env" placeholder="go version go1.26.0 linux/amd64（选填）"></label>
    <label>任务名（可选）<input id="c_name"></label>
  </div>
  <label>基线仓库目录（已提交初始代码、已配好远端的仓库）<input id="c_baseline" placeholder="D:\work\my-task-repo"></label>
  <label>验收命令（每行一条，仅记录结果，不影响证据；如 go test ./...）<textarea id="c_checks"></textarea></label>
  <div class="btns"><button onclick="createJob()">创建并准备（快照基线 + 复制 A/B 工作区）</button>
  <button class="ghost" onclick="hideCreate()">取消</button></div>
</div>

<div id="batchPanel" class="card" style="display:none">
  <h2 style="margin-top:0">批量导入</h2>
  <p class="muted">每行一条任务，格式：<b>提示词</b>；或 TSV：<b>提示词⇥任务类型⇥难度⇥语言/框架⇥基线仓库目录</b>。所有任务共用下方基线目录（TSV 第 5 列可逐行覆盖）。</p>
  <textarea id="b_lines" style="min-height:160px" placeholder="实现一个支持事件时间和故障恢复的流处理引擎&#10;实现一个限流器库	0-1代码生成	地狱	Go, Redis	D:\work\task-2"></textarea>
  <div class="grid3">
    <label>默认任务类型<select id="b_task_type"></select></label>
    <label>默认难度<select id="b_difficulty"></select></label>
    <label>默认语言/框架<input id="b_stack"></label>
    <label>默认基线仓库（每行未指定时用它）<input id="b_baseline"></label>
  </div>
  <div class="btns"><button onclick="createBatch()">批量创建 → 全部准备 → 入队运行</button>
  <button class="ghost" onclick="hideBatch()">取消</button></div>
</div>

<div id="settingsPanel" class="card" style="display:none">
  <h2 style="margin-top:0">设置</h2>
  <div class="grid">
    <div class="grid3" style="grid-column:1/-1">
      <label>claude 命令<input id="s_claude"></label>
      <label>最多并行 pair 数<input id="s_parallel" type="number" min="1" max="6"></label>
      <label>单侧超时（秒）<input id="s_timeout" type="number"></label>
      <label>OSS Endpoint<input id="s_endpoint"></label>
      <label>OSS 区域<input id="s_region"></label>
      <label>OSS Bucket<input id="s_bucket"></label>
      <label>公网访问基址（留空用 path-style）<input id="s_pubbase"></label>
      <label>对象 key 前缀<input id="s_prefix"></label>
      <label>默认标注员<input id="s_reviewer"></label>
    </div>
  </div>
  <div class="card" style="background:#f8fafc;margin:10px 0">
    <b>京东云密钥</b>（只放在 <span class="mono" id="secretPath"></span>，不进仓库）<br>
    <span class="muted">文件内容两行：OSS_ACCESS_KEY_ID=... 和 OSS_SECRET_ACCESS_KEY=...</span>
    <div class="kv" id="ossStatus" style="margin-top:6px"></div>
    <div class="btns">
      <button class="sec" onclick="saveSettings()">保存设置</button>
      <button class="ghost" onclick="ossTest()">测试连接 / 列出 bucket</button>
      <button class="ghost" onclick="ossCreate()">创建 bucket（不存在时）</button>
    </div>
  </div>
  <button class="ghost" onclick="hideSettings()">关闭</button>
</div>

<div class="card">
  <h2 style="margin:0 0 8px">任务队列</h2>
  <table><thead><tr><th>状态</th><th>任务</th><th>类型/难度</th><th>A</th><th>B</th><th>证据</th><th>GSB</th><th></th></tr></thead>
  <tbody id="jobRows"><tr><td colspan="8" class="muted">加载中…</td></tr></tbody></table>
</div>
</div>

<div id="detail" style="display:none"></div>
<div class="toast" id="toast"></div>

<script>
const $=id=>document.getElementById(id);
let JOBS=[], SEL=null, SETTINGS={}, pollTimer=null;

function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function toast(m,bad){const t=$("toast");t.textContent=m;t.style.background=bad?"#7f1d1d":"#111827";t.style.display="block";setTimeout(()=>t.style.display="none",4000)}
async function api(path,body){
  const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:body?JSON.stringify(body):"{}"});
  const x=await r.json().catch(()=>({error:"bad json"})); if(!r.ok||x.ok===false){toast((x.error||"请求失败"),true);throw x} return x;
}
function fillSelect(el,vals,cur){el.innerHTML=vals.map(v=>`<option ${v===cur?"selected":""}>${v}</option>`).join("")}
function initSelects(){[["c_task_type","b_task_type"]].forEach(pair=>{})}
function sideStatus(s){const map={pending:"待运行",preparing:"准备中",running:"运行中",collecting:"采集中",done:"完成",failed:"失败"};
  const cls=s==="done"?"g":s==="failed"?"r":s==="running"?"y":"r";
  return `<span class="dot ${cls}"></span>${map[s]||s}`}

async function loadSettings(){SETTINGS=await api("/api/settings_get");
  $("s_claude").value=SETTINGS.claude_command;$("s_parallel").value=SETTINGS.max_parallel_pairs;
  $("s_timeout").value=SETTINGS.side_timeout_seconds;$("s_endpoint").value=SETTINGS.oss_endpoint;
  $("s_region").value=SETTINGS.oss_region;$("s_bucket").value=SETTINGS.oss_bucket;
  $("s_pubbase").value=SETTINGS.oss_public_base;$("s_prefix").value=SETTINGS.oss_key_prefix;
  $("s_reviewer").value=SETTINGS.reviewer;$("secretPath").textContent=SETTINGS.secret_path;
  const st=SETTINGS.oss;$("ossStatus").innerHTML=st.configured
    ?`已配置：${esc(st.endpoint)} / bucket=<b>${esc(st.bucket)}</b> / key=${esc(st.access_key_id_preview)}`
    :`<span class="bad">未配置（endpoint/bucket 或密钥缺失）</span>`;
  fillSelect($("c_task_type"),__TASK_TYPES__,SETTINGS.default_task_type);
  fillSelect($("b_task_type"),__TASK_TYPES__,SETTINGS.default_task_type);
  fillSelect($("c_difficulty"),__DIFF__,SETTINGS.default_difficulty);
  fillSelect($("b_difficulty"),__DIFF__,SETTINGS.default_difficulty);
}
async function saveSettings(){const body={claude_command:$("s_claude").value,max_parallel_pairs:+$("s_parallel").value,
 side_timeout_seconds:+$("s_timeout").value,oss_endpoint:$("s_endpoint").value,oss_region:$("s_region").value,
 oss_bucket:$("s_bucket").value,oss_public_base:$("s_pubbase").value,oss_key_prefix:$("s_prefix").value,
 reviewer:$("s_reviewer").value};SETTINGS=await api("/api/settings_save",body);toast("设置已保存")}
async function ossTest(){const x=await api("/api/oss_test",{endpoint:$("s_endpoint").value,region:$("s_region").value,bucket:$("s_bucket").value});
 toast(x.ok?("连接成功，bucket: "+(x.buckets||[]).join(", ")+(x.bucket_present?"（目标 bucket 存在）":"（目标 bucket 不存在，可点创建）")):("失败: "+x.error),!x.ok)}
async function ossCreate(){const x=await api("/api/oss_create",{endpoint:$("s_endpoint").value,region:$("s_region").value,bucket:$("s_bucket").value});
 toast(x.existed?"bucket 已存在":"bucket 已创建: "+x.name)}

function showCreate(){$("createPanel").style.display="block";$("batchPanel").style.display="none"}
function hideCreate(){$("createPanel").style.display="none"}
function showBatch(){$("batchPanel").style.display="block";$("createPanel").style.display="none"}
function hideBatch(){$("batchPanel").style.display="none"}
function openSettings(){$("settingsPanel").style.display="block"}
function hideSettings(){$("settingsPanel").style.display="none"}

async function createJob(){
  const body={prompt:$("c_prompt").value,task_type:$("c_task_type").value,difficulty:$("c_difficulty").value,
   stack:$("c_stack").value,repro_level:$("c_repro").value,env_desc:$("c_env").value,name:$("c_name").value,
   baseline_repo:$("c_baseline").value,check_commands:$("c_checks").value};
  await api("/api/job_create",body);$("c_prompt").value="";hideCreate();toast("已创建并开始准备");loadJobs()}
async function createBatch(){
  const body={lines:$("b_lines").value,task_type:$("b_task_type").value,difficulty:$("b_difficulty").value,
   stack:$("b_stack").value,baseline_repo:$("b_baseline").value};
  const x=await api("/api/job_batch",body);toast(`已创建 ${x.created.length} 条，准备完成 ${x.prepared.length} 条`);$("b_lines").value="";hideBatch();loadJobs()}

async function loadJobs(){JOBS=(await api("/api/jobs")).jobs;renderRows();if(SEL)renderDetail(SEL)}
function renderRows(){
  $("jobRows").innerHTML=JOBS.map(j=>{const a=j.sides.A,b=j.sides.B;
   const ev=[a.jsonl_local,b.jsonl_local,a.video_url||a.video_local,b.video_url||b.video_local].filter(Boolean).length;
   const gsb=j.review.conclusion?`<b>${esc(j.review.conclusion)}</b>`:'<span class="muted">待标注</span>';
   return `<tr><td><span class="tag t-${j.status}">${stName(j.status)}</span></td>
   <td><a href="#" onclick="renderDetail('${j.id}');return false">${esc(j.name)}</a><div class="kv">${esc(j.task_type)} · ${esc(j.difficulty)} · ${esc(j.stack)}</div></td>
   <td>${esc(j.task_type)}<br><span class="kv">${esc(j.difficulty)}</span></td>
   <td>${sideStatus(a.status)}<div class="kv">${esc((a.session_id||"").slice(0,8))}</div></td>
   <td>${sideStatus(b.status)}<div class="kv">${esc((b.session_id||"").slice(0,8))}</div></td>
   <td>${ev}/4</td><td>${gsb}</td>
   <td><button class="ghost" onclick="renderDetail('${j.id}')">打开</button></td></tr>`}).join("") || '<tr><td colspan="8" class="muted">还没有任务</td></tr>';
}
function stName(s){return {draft:"草稿",ready:"待运行",running:"运行中",evidence_ready:"待标注",failed:"有失败",done:"已完成"}[s]||s}

function renderDetail(id){SEL=id;const j=JOBS.find(x=>x.id===id);if(!j)return;
  const D=$("detail");D.style.display="block";
  const c=j.check||{items:[],ready:false,blocking_count:0,warning_count:0};
  D.innerHTML=`<div class="card">
    <div class="btns" style="margin-top:0"><button class="ghost" onclick="closeDetail()">← 返回列表</button>
    <span style="font-weight:700;font-size:15px;align-self:center">${esc(j.name)}</span><span class="sp" style="flex:1"></span>
    <span class="tag t-${j.status}">${stName(j.status)}</span></div>
    <div class="kv" style="margin:6px 0">基线：${j.baseline_url?`<a href="${esc(j.baseline_url)}" target="_blank" class="mono">${esc(j.baseline_sha.slice(0,12))}</a>`:"未准备"} · ${esc(j.harness)} ${esc(j.harness_version)} · ${esc(j.os_name)}</div>
    <div class="grid">
      ${sideHtml(j,"A")}${sideHtml(j,"B")}
    </div>
    <div class="btns">
      <button onclick="act('prepare')">① 重新准备/校验基线</button>
      <button class="sec" onclick="act('enqueue')">② 开始/重试运行</button>
      <button class="ghost" onclick="refreshDetail()">↻ 刷新检查</button>
      <button class="ghost" onclick="act('collect')">重新采集会话</button>
    </div>
    <div style="margin-top:8px"><label>A 侧录屏文件（mp4，本地路径，也可点右侧选择）</label>
      <div style="display:flex;gap:8px"><input id="vA" value="${esc(j.sides.A.video_local||"")}" placeholder="D:\videos\a.mp4">
      <button class="ghost" type="button" onclick="pickVideo('A')">选择…</button></div></div>
    <div style="margin-top:8px"><label>B 侧录屏文件</label>
      <div style="display:flex;gap:8px"><input id="vB" value="${esc(j.sides.B.video_local||"")}" placeholder="D:\videos\b.mp4">
      <button class="ghost" type="button" onclick="pickVideo('B')">选择…</button></div></div>
    <div class="btns">
      <button class="sec" onclick="act('set_videos')">保存录屏路径</button>
      <button onclick="act('upload')">③ 上传轨迹+录屏到 OSS</button>
    </div>
  </div>

  <div class="card"><h3 style="margin-top:0">完整度检查 ${c.ready?'<span class="ok">✓ 可导出</span>':`（阻塞 ${c.blocking_count} / 提醒 ${c.warning_count}）`}</h3>
    <div id="checks">${checksHtml(c.items)}</div>
    <button class="ghost" onclick="refreshDetail(true)" style="margin-top:8px">在线核验链接可访问性（较慢）</button>
  </div>

  <div class="card"><h3 style="margin-top:0">④ GSB 人工判断（严禁 AI 代写）</h3>
    <div class="grid3">
      <label>结论<select id="r_conclusion"><option value="">请选择</option>${__CONCLUSIONS__.map(x=>`<option ${x===j.review.conclusion?"selected":""}>${x}</option>`).join("")}</select></label>
      <label>标注员<input id="r_reviewer" value="${esc(j.review.reviewer||"")}"></label>
      <label>备注<input id="r_note" value="${esc(j.review.note||"")}"></label>
    </div>
    <label>GSB 理由（A、B 分别说明；Same 至少 80 字，其余 30 字以上）<textarea id="r_reason" style="min-height:140px">${esc(j.review.reason||"")}</textarea></label>
    <div class="btns"><button onclick="saveReview()">保存 GSB</button>
    <button class="sec" onclick="exportRow()" ${c.ready?"":"disabled"}>⑤ 生成 TSV（全部绿灯后可用）</button></div>
    <div id="tsvBox" style="display:none;margin-top:10px">
      <pre class="log" id="tsvPre"></pre>
      <div class="btns"><button onclick="copyTsv()">复制 TSV（粘贴到飞书表格）</button>
      <button class="ghost" onclick="downloadTsv()">下载 .tsv 文件</button></div>
    </div>
  </div>`;
  D.scrollIntoView({behavior:"smooth"});
}
function sideHtml(j,s){const x=j.sides[s];const run=x.status==="running";
  return `<div class="sidebox"><h3>${s} 侧 ${sideStatus(x.status)}</h3>
  <div class="kv">工作区：${esc(x.workspace||"未准备")}<br>分支：${esc(x.branch)}<br>
  会话：${esc(x.session_id||"-")}<br>
  产物：${x.head_url?`<a href="${esc(x.head_url)}" target="_blank" class="mono">${esc(x.head_sha.slice(0,12))}</a>${x.pushed?" ✓push":" ✗未push"}`:"-"}<br>
  轨迹：${x.trace_url?`<a href="${esc(x.trace_url)}" target="_blank">链接</a>`:(x.jsonl_local?esc(x.jsonl_local.split("\\").pop()):"-")}<br>
  录屏：${x.video_url?`<a href="${esc(x.video_url)}" target="_blank">链接</a>`:(x.video_local?esc(x.video_local.split("\\").pop()):"缺失")}<br>
  ${x.error?`<span class="bad">${esc(x.error)}</span>`:""}</div>
  <div class="btns"><button class="ghost" onclick="sideAct('retry','${s}')">重跑该侧</button>
  <button class="ghost" ${run?"":"disabled"} onclick="sideAct('abort','${s}')">中止</button>
  <button class="ghost" onclick="openLog('${s}')">运行日志</button></div></div>`}
function checksHtml(items){if(!items||!items.length)return '<span class="muted">点「刷新检查」</span>';
  const groups={};items.forEach(i=>{(groups[i.group]=groups[i.group]||[]).push(i)});
  return Object.entries(groups).map(([g,xs])=>`<div style="margin:6px 0"><b>${esc(g)}</b><br>${xs.map(x=>
   `<span title="${esc(x.detail)}"><span class="dot ${x.ok?"g":x.blocking?"r":"y"}"></span><span class="${x.ok?"ok":x.blocking?"bad":"warn"}">${esc(x.label)}</span></span>`).join("　")}</div>`).join("")}
function closeDetail(){$("detail").style.display="none";SEL=null}
async function act(a){const body={job:SEL,action:a,video_a:$("vA")? $("vA").value:"",video_b:$("vB")?$("vB").value:""};
  await api("/api/job_action",body);await refreshDetail()}
async function sideAct(a,s){await api("/api/side_action",{job:SEL,side:s,action:a});await refreshDetail()}
async function refreshDetail(online){const j=await api("/api/job",{id:SEL,online:!!online});const f=JOBS.findIndex(x=>x.id===SEL);if(f>=0)JOBS[f]=j;renderRows();renderDetail(SEL)}
async function saveReview(){await api("/api/review",{job:SEL,conclusion:$("r_conclusion").value,reason:$("r_reason").value,reviewer:$("r_reviewer").value,note:$("r_note").value});toast("GSB 已保存");refreshDetail()}
async function exportRow(){const x=await api("/api/export",{job:SEL});$("tsvBox").style.display="block";$("tsvPre").textContent=x.tsv;window.__tsv=x.tsv}
function copyTsv(){navigator.clipboard.writeText(window.__tsv||"");toast("已复制，去飞书表格粘贴（整行）")}
function downloadTsv(){const b=new Blob([window.__tsv||""],{type:"text/tab-separated-values"});const a=document.createElement("a");a.href=URL.createObjectURL(b);a.download=SEL+".tsv";a.click()}
async function openLog(s){const x=await api("/api/log",{job:SEL,side:s});const w=window.open("","_blank");w.document.write(`<pre style="font:12px Consolas;white-space:pre-wrap">${esc(x.log||"(空)")}</pre>`)}
async function pickVideo(s){const x=await api("/api/pick_file",{side:s});if(x.path){$("v"+s).value=x.path}}

loadSettings().then(loadJobs);
pollTimer=setInterval(()=>{if(JOBS.some(j=>["running","ready"].includes(j.status)||Object.values(j.sides).some(x=>["running","preparing"].includes(x.status))))loadJobs()},5000);
</script></body></html>"""


class DeskServer:
    def __init__(self, store: DeskStore, port: int = 8765):
        self.store = store
        self.runner = PairRunner(store)
        self.port = port

    def oss_cfg(self, overrides: dict | None = None) -> oss_mod.OssConfig | None:
        settings = dict(self.store.settings())
        if overrides:
            for key in ("endpoint", "region", "bucket"):
                val = overrides.get(key)
                if val:
                    settings[{"endpoint": "oss_endpoint", "region": "oss_region", "bucket": "oss_bucket"}[key]] = val
        return oss_mod.config_from_mapping(settings, self.store.secrets_path)

    # ---------- actions ----------
    def job_create(self, body: dict, prepare: bool = True) -> dict:
        job = self.store.create_job(body)
        result = {"job": job["id"], "prepared": []}
        if prepare and body.get("baseline_repo"):
            try:
                self.runner.prepare(job["id"])
                result["prepared"].append(job["id"])
                self.runner.enqueue(job["id"])
            except Exception as exc:
                self.store.update_job(job["id"], {"status": "failed", "error": str(exc)})
                result["prepare_error"] = str(exc)
        return result

    def job_batch(self, body: dict) -> dict:
        import csv as _csv
        import io as _io
        created: list[str] = []
        prepared: list[str] = []
        errors: list[str] = []
        default_baseline = (body.get("baseline_repo") or "").strip()
        for raw in _io.StringIO(body.get("lines", "")):
            line = raw.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            row = next(_csv.reader(_io.StringIO(line), delimiter="\t"))
            data = {
                "prompt": row[0].strip(),
                "task_type": (row[1] if len(row) > 1 and row[1].strip() else body.get("task_type")),
                "difficulty": (row[2] if len(row) > 2 and row[2].strip() else body.get("difficulty")),
                "stack": (row[3] if len(row) > 3 and row[3].strip() else body.get("stack")),
                "baseline_repo": (row[4] if len(row) > 4 and row[4].strip() else default_baseline),
            }
            if not data["prompt"] or not data["baseline_repo"]:
                errors.append(f"跳过（缺提示词或基线目录）: {data['prompt'][:30]}")
                continue
            res = self.job_create(data, prepare=True)
            created.append(res["job"])
            if res["prepared"]:
                prepared.append(res["job"])
            if res.get("prepare_error"):
                errors.append(f"{res['job']}: {res['prepare_error']}")
        return {"created": created, "prepared": prepared, "errors": errors}

    def job_action(self, body: dict) -> dict:
        job_id, action = body["job"], body["action"]
        if action == "prepare":
            return {"result": self.runner.prepare(job_id)}
        if action == "enqueue":
            job = self.store.get_job(job_id)
            if not job.get("baseline_sha"):
                self.runner.prepare(job_id)
            self.store.update_job(job_id, {"status": "ready", "error": ""})
            for side_name, side in self.store.get_job(job_id)["sides"].items():
                if side["status"] == "failed":
                    self.runner.retry_side(job_id, side_name)
            self.runner.enqueue(job_id)
            return {"queued": True}
        if action == "collect":
            for side_name in ("A", "B"):
                self._recollect_side(job_id, side_name)
            return {"collected": True}
        if action == "set_videos":
            for side_name, key in (("A", "video_a"), ("B", "video_b")):
                val = (body.get(key) or "").strip()
                if val:
                    self.store.update_side(job_id, side_name, {"video_local": val})
            return {"saved": True}
        if action == "upload":
            cfg = self.oss_cfg()
            if cfg is None:
                raise RuntimeError("OSS 未配置（检查设置里的 endpoint/bucket 与 secrets.env）")
            job = self.store.get_job(job_id)
            for side_name in ("A", "B"):
                urls = upload_side(self.store, job, side_name, cfg)
                self.store.update_side(job_id, side_name, {
                    k: v for k, v in urls.items() if k.endswith("url")
                })
            return {"uploaded": True}
        raise RuntimeError(f"未知操作 {action}")

    def _recollect_side(self, job_id: str, side_name: str) -> None:
        import shutil
        job = self.store.get_job(job_id)
        side = job["sides"][side_name]
        wdir = side.get("workspace", "")
        if not wdir or not Path(wdir).is_dir():
            return
        sessions = ws.find_session_jsonl(wdir)
        if not sessions:
            return
        chosen = sessions[0]
        kept = self.store.evidence_dir(job_id) / f"{side_name.lower()}-{chosen['session_id']}.jsonl"
        shutil.copyfile(chosen["path"], kept)
        self.store.update_side(job_id, side_name, {
            "session_id": chosen["session_id"], "jsonl_local": str(kept),
        })

    def side_action(self, body: dict) -> dict:
        job_id, side_name, action = body["job"], body["side"], body["action"]
        if action == "retry":
            self.runner.retry_side(job_id, side_name)
            return {"retry": True}
        if action == "abort":
            self.runner.abort_side(job_id, side_name)
            return {"aborted": True}
        raise RuntimeError(f"未知操作 {action}")

    def job_view(self, job_id: str, online: bool = False) -> dict:
        job = self.store.get_job(job_id)
        job["check"] = run_checklist(job, online=online)
        return job

    # ---------- http handler ----------
    def make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):  # quiet
                pass

            def _send(self, value, status=200):
                data = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length", "0"))
                if not n:
                    return {}
                try:
                    return json.loads(self.rfile.read(n).decode("utf-8"))
                except json.JSONDecodeError:
                    return {}

            def do_GET(self):
                path = urllib.parse.urlparse(self.path).path
                if path == "/":
                    data = PAGE.replace("__TASK_TYPES__", json.dumps(TASK_TYPES, ensure_ascii=False)) \
                              .replace("__DIFF__", json.dumps(DIFFICULTIES, ensure_ascii=False)) \
                              .replace("__CONCLUSIONS__", json.dumps(CONCLUSIONS, ensure_ascii=False))
                    raw = data.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                self._send({"error": "not found"}, 404)

            def do_POST(self):
                path = urllib.parse.urlparse(self.path).path
                body = self._body()
                try:
                    self._send(self._route(path, body))
                except FileNotFoundError as exc:
                    self._send({"ok": False, "error": str(exc)}, 404)
                except Exception as exc:
                    self._send({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400)

            def _route(self, path: str, body: dict):
                if path == "/api/settings_get":
                    s = server.store.settings()
                    return {**s, "secret_path": str(server.store.secrets_path),
                            "oss": oss_mod.describe_config(server.oss_cfg())}
                if path == "/api/settings_save":
                    return server.store.save_settings(body)
                if path == "/api/oss_test":
                    cfg = server.oss_cfg(body)
                    return oss_mod.connection_test(cfg) if cfg else {"ok": False, "error": "OSS 配置不完整"}
                if path == "/api/oss_create":
                    cfg = server.oss_cfg(body)
                    if cfg is None:
                        return {"ok": False, "error": "OSS 配置不完整"}
                    return oss_mod.ensure_bucket(cfg)
                if path == "/api/jobs":
                    return {"jobs": server.store.list_jobs()}
                if path == "/api/job_create":
                    return server.job_create(body)
                if path == "/api/job_batch":
                    return server.job_batch(body)
                if path == "/api/job":
                    return server.job_view(body["id"], bool(body.get("online")))
                if path == "/api/job_action":
                    return server.job_action(body)
                if path == "/api/side_action":
                    return server.side_action(body)
                if path == "/api/review":
                    return server.store.update_review(body["job"], body)
                if path == "/api/export":
                    job = server.store.get_job(body["job"])
                    out = server.store.evidence_dir(body["job"]) / "submission.tsv"
                    return export_tsv(job, out, strict=True)
                if path == "/api/log":
                    p = server.store.evidence_dir(body["job"]) / f"{body['side'].lower()}-run.log"
                    return {"log": p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""}
                if path == "/api/pick_file":
                    return server._pick_file(body.get("side", "A"))
                return {"ok": False, "error": f"unknown path {path}"}

        return Handler

    def _pick_file(self, side: str) -> dict:
        """Native file dialog via PowerShell (runs on the desktop, not a service)."""
        import subprocess
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$f=New-Object System.Windows.Forms.OpenFileDialog;"
            "$f.Filter='Video (*.mp4)|*.mp4|All (*.*)|*.*';"
            "if($f.ShowDialog() -eq 'OK'){[Console]::OutputEncoding=[Text.Encoding]::UTF8;$f.FileName}"
        )
        p = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        return {"path": p.stdout.strip()}

    def serve(self, open_browser: bool = True) -> None:
        self.runner.start()
        httpd = ThreadingHTTPServer(("127.0.0.1", self.port), self.make_handler())
        url = f"http://127.0.0.1:{self.port}/"
        if open_browser:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        print(f"Pair 交付台运行中: {url}\n数据目录: {self.store.home}\nCtrl+C 退出（任务状态已落盘，重开自动恢复）")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.runner.stop()
            httpd.server_close()


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="atk desk")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    DeskServer(DeskStore(), port=args.port).serve(open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
