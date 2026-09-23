"""Pair Desk — local browser console for the pairwise GSB workflow.

Run with:  python -m agent_trace_kit.desk  (or  atk desk)
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import ghutil
from . import oss as oss_mod
from . import ports as ports_mod
from . import workspace as ws
from .checklist import run_checklist
from .desk_store import CONCLUSIONS, DIFFICULTIES, REPRO_LEVELS, TASK_TYPES, VALIDITY, DeskStore, clean_path
from .export_tsv import export_tsv, upload_side
from .recorder import Recorder
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
a.ghostbtn{display:inline-block;padding:8px 13px;border-radius:6px;background:#e5e7eb;color:#111;text-decoration:none;font-size:14px}
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
.tabs{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.tabbtn{background:#e5e7eb;color:#374151;font-weight:600;padding:6px 14px;border-radius:8px 8px 0 0;border:1px solid var(--line);border-bottom:0}
.tabbtn.active{background:var(--card);color:var(--blue);box-shadow:0 -2px 0 var(--blue) inset}
.badge{display:inline-block;min-width:18px;padding:0 6px;border-radius:20px;background:#cbd5e1;color:#1f2937;font-size:11px;font-weight:700;text-align:center;margin-left:5px}
.tabbtn.active .badge{background:#dbeafe;color:var(--blue)}
.rowarch{opacity:.55}.rowarch:hover{opacity:1}
.t-archived{background:#e2e8f0;color:#475569}
</style></head><body>
<header><h1>Pair 交付台</h1><span class="muted" style="color:#94a3b8">同模型 · 同环境 · 同提示词 · A/B 双跑</span><span class="sp"></span>
<a class="ghostbtn" href="/settings">⚙ 后台设置</a>
<button class="ghost" onclick="loadJobs()">🔄 刷新</button></header>
<div class="wrap">

<div class="card">
  <div class="btns" style="margin-top:0">
    <button onclick="showCreate()">＋ 新建任务</button>
    <button class="sec" onclick="showBatch()">📋 批量导入提示词</button>
    <span class="muted" style="align-self:center">并行队列自动运行；你只需要录产物视频 + 写 GSB。</span>
  </div>
</div>

<div class="card" id="portHubCard">
  <h2 style="margin:0 0 8px">本机端口（全部任务）
    <span class="muted" style="font-weight:400"> · 预览和 npm 残留都在这里关，不要打开 8080 上已有的页</span></h2>
  <div id="portHubBody" class="kv muted">扫描中…</div>
  <div class="btns">
    <button class="ghost" type="button" onclick="portHubRefresh()">重新扫描</button>
    <button class="sec" type="button" onclick="portsReapAll()">停止全部预览并清理残留</button>
  </div>
</div>

<div id="createPanel" class="card" style="display:none">
  <h2 style="margin-top:0">新建一条 pair 任务</h2>
  <label>User Prompt（完整原文，A/B 共用）</label><textarea id="c_prompt" style="min-height:150px"></textarea>
  <div class="grid3">
    <label>任务类型<select id="c_task_type"></select></label>
    <label>难度<select id="c_difficulty"></select></label>
    <label>语言/框架<input id="c_stack" placeholder="Go, Gin / Python, FastAPI"></label>
    <label>环境可复现等级<select id="c_repro"></select></label>
    <label>任务名（可选）<input id="c_name"></label>
  </div>
  <div class="grid3">
    <label style="grid-column:span 2">GitHub 仓库名（本地文件夹同名，字母数字 - _ .）
      <input id="c_repo" placeholder="my-task-repo" oninput="repoCheck()"></label>
    <label>仓库可见性<select id="c_private">
      <option value="0">公开（评测方可直接访问，默认）</option>
      <option value="1">私有</option>
    </select></label>
  </div>
  <label>README 说明文字（可选，留空写占位说明）<input id="c_readme" placeholder="一句话说明这个题目的初始仓库"></label>
  <div id="repoHint" class="kv" style="margin:4px 0 8px"></div>
  <details><summary class="muted" style="cursor:pointer">高级：自定义基线父目录 / 直接使用已有的本地仓库</summary>
    <div class="grid3" style="margin-top:8px">
      <label>基线父目录（留空用后台默认）<input id="c_parent" oninput="repoCheck()"></label>
      <label style="grid-column:1/-1">已有本地基线仓库目录（填写后跳过自动建仓，直接快照）<input id="c_baseline" placeholder="D:\work\my-task-repo"></label>
    </div>
  </details>
  <label>验收命令（每行一条，仅记录结果，不影响证据；如 go test ./...）<textarea id="c_checks"></textarea></label>
  <div class="btns"><button onclick="createJob(event)">创建：建 GitHub 空仓 + 初始化 main → 复制 A/B 工作区 → 自动开跑</button>
  <button class="ghost" onclick="hideCreate()">取消</button></div>
</div>

<div id="batchPanel" class="card" style="display:none">
  <h2 style="margin-top:0">批量导入</h2>
  <p class="muted">每行一条任务，格式：<b>提示词</b>；或 TSV：<b>提示词⇥任务类型⇥难度⇥语言/框架⇥GitHub 仓库名</b>（第 5 列也可填本地已有仓库的完整路径，会自动判别）。仓库在下方父目录下同名创建并自动建 GitHub 空仓。</p>
  <textarea id="b_lines" style="min-height:160px" placeholder="实现一个支持事件时间和故障恢复的流处理引擎&#10;实现一个限流器库	0-1代码生成	地狱	Go, Redis	rate-limiter-lib"></textarea>
  <div class="grid3">
    <label>默认任务类型<select id="b_task_type"></select></label>
    <label>默认难度<select id="b_difficulty"></select></label>
    <label>默认语言/框架<input id="b_stack"></label>
    <label>基线父目录（新仓库的本地文件夹建在这里）<input id="b_parent"></label>
  </div>
  <div class="btns"><button onclick="createBatch()">批量创建 → 全部准备 → 入队运行</button>
  <button class="ghost" onclick="hideBatch()">取消</button></div>
</div>

<div class="card">
  <div class="tabs" style="margin-bottom:0">
    <h2 style="margin:0 8px 0 0">任务队列</h2>
    <button id="tabActive" class="tabbtn active" onclick="setView('active')">待办<span id="cntActive" class="badge">0</span></button>
    <button id="tabArch" class="tabbtn" onclick="setView('archived')">已归档<span id="cntArch" class="badge">0</span></button>
    <span style="flex:1"></span>
    <input id="jobFilter" oninput="renderRows()" placeholder="🔍 搜索任务名 / 类型 / 仓库"
           style="width:240px;font-weight:400">
  </div>
  <table style="margin-top:10px"><thead><tr><th>状态</th><th>任务</th><th>类型/难度</th><th>A</th><th>B</th><th>证据</th><th>GSB</th><th></th></tr></thead>
  <tbody id="jobRows"><tr><td colspan="8" class="muted">加载中…</td></tr></tbody></table>
  <p class="kv" style="margin:8px 2px 0">归档只从默认队列隐藏，不删除证据；切到「已归档」可随时恢复。运行中的任务需先结束才能归档。</p>
</div>
</div>

<div id="detail" style="display:none"></div>
<div id="follow" style="display:none;position:fixed;inset:0;background:rgba(15,23,42,.55);z-index:50" onclick="if(event.target===this)closeFollow()">
  <div style="background:#0f172a;color:#e2e8f0;border-radius:10px;margin:24px auto;max-width:1400px;height:calc(100vh - 48px);display:flex;flex-direction:column;padding:14px 16px">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
      <b style="font-size:15px">📡 实时跟随 · <span id="followTitle"></span></b>
      <span class="muted" style="color:#94a3b8;font-size:12px">只读旁观，不会影响后台任务；日志每 2 秒刷新</span>
      <span style="flex:1"></span>
      <label style="margin:0;font-weight:400;color:#cbd5e1;font-size:12px"><input type="checkbox" id="followAutoscroll" checked style="width:auto;margin-right:5px">自动滚到底</label>
      <button class="ghost" onclick="clearFollow()">清屏</button>
      <button class="ghost" onclick="closeFollow()">关闭</button>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;flex:1;min-height:0">
      <div style="display:flex;flex-direction:column;min-height:0">
        <div style="color:#7dd3fc;font-weight:600;margin-bottom:4px">A 侧 <span id="followStateA" class="muted" style="font-weight:400"></span></div>
        <pre id="followLogA" style="flex:1;margin:0;background:#020617;border-radius:8px;padding:10px;overflow:auto;white-space:pre-wrap;font:12px/1.5 Consolas,monospace"></pre>
      </div>
      <div style="display:flex;flex-direction:column;min-height:0">
        <div style="color:#7dd3fc;font-weight:600;margin-bottom:4px">B 侧 <span id="followStateB" class="muted" style="font-weight:400"></span></div>
        <pre id="followLogB" style="flex:1;margin:0;background:#020617;border-radius:8px;padding:10px;overflow:auto;white-space:pre-wrap;font:12px/1.5 Consolas,monospace"></pre>
      </div>
    </div>
  </div>
</div>
<div id="delModal" style="display:none;position:fixed;inset:0;background:rgba(15,23,42,.55);z-index:60">
  <div style="background:#fff;border-radius:10px;margin:16vh auto;max-width:480px;padding:18px 20px">
    <h3 style="margin-top:0">删除任务</h3>
    <p id="delWarn" style="margin:6px 0"></p>
    <div id="delOpts" style="margin:10px 0"></div>
    <div class="btns"><button style="background:#b91c1c" onclick="doDelete()">确认删除</button>
    <button class="ghost" onclick="$('delModal').style.display='none'">取消</button></div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const $=id=>document.getElementById(id);
let JOBS=[], SEL=null, SETTINGS={}, pollTimer=null, jobsInflight=false, CHECKS={}, JOB_VIEW="active";

function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function toast(m,bad){const t=$("toast");t.textContent=m;t.style.background=bad?"#7f1d1d":"#111827";t.style.display="block";setTimeout(()=>t.style.display="none",4000)}
async function api(path,body){
  const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:body?JSON.stringify(body):"{}"});
  const x=await r.json().catch(()=>({error:"bad json"})); if(!r.ok||x.ok===false){toast((x.error||"请求失败"),true);throw x} return x;
}
function fillSelect(el,vals,cur){el.innerHTML=vals.map(v=>`<option ${v===cur?"selected":""}>${v}</option>`).join("")}
function initSelects(){[["c_task_type","b_task_type"]].forEach(pair=>{})}
function sideBusy(x){return !!(x&&x.live)||["running","preparing","collecting"].includes(x&&x.status)}
function sideStatus(s, live){
  if(live && !["collecting","done"].includes(s||"")) s="running";
  const map={pending:"待运行",preparing:"准备中",running:"运行中",collecting:"采集中",done:"完成",failed:"失败"};
  const cls=s==="done"?"g":s==="failed"?"r":(s==="running"||s==="preparing"||s==="collecting")?"y":"r";
  return `<span class="dot ${cls}"></span>${map[s]||s}`}

async function loadDefaults(){SETTINGS=await api("/api/settings_get");
  fillSelect($("c_task_type"),__TASK_TYPES__,SETTINGS.default_task_type);
  fillSelect($("b_task_type"),__TASK_TYPES__,SETTINGS.default_task_type);
  fillSelect($("c_difficulty"),__DIFF__,SETTINGS.default_difficulty);
  fillSelect($("b_difficulty"),__DIFF__,SETTINGS.default_difficulty);
  fillSelect($("c_repro"),__REPRO__,SETTINGS.default_repro_level);
  $("c_parent").placeholder=SETTINGS.baseline_parent_dir||"";
  $("c_parent").value=$("c_parent").value||"";
  $("c_private").value=SETTINGS.github_private?"1":"0";
  $("b_parent").value=SETTINGS.baseline_parent_dir||"";
  repoCheck();
}

let repoCheckTimer=null;
function repoCheck(){
  const name=$("c_repo").value.trim(), hint=$("repoHint");
  clearTimeout(repoCheckTimer);
  if(!name){hint.innerHTML="";return;}
  const local=(($("c_parent").value.trim()||SETTINGS.baseline_parent_dir||"")+"\\"+name);
  hint.textContent="查询中… 本地将创建于 "+local;
  repoCheckTimer=setTimeout(async()=>{
    try{
      const x=await api("/api/repo_check",{name});
      if(x.syntax_error){hint.innerHTML='<span class="bad">✗ '+esc(x.syntax_error)+'</span>';return;}
      if(x.gh_error){hint.innerHTML='<span class="bad">✗ '+esc(x.gh_error)+'</span><br>本地将创建于 '+esc(local);return;}
      if(x.exists){hint.innerHTML='<span class="warn">⚠ 远端已存在 <b>'+esc(x.full_name)+'</b>（'+esc(x.visibility.toLowerCase())+'），将直接复用并推送 main，不会重建</span><br>本地：'+esc(local);}
      else{hint.innerHTML='<span class="ok">✓ 名称可用，将在 GitHub 新建 <b>'+esc(x.full_name)+'</b> 空仓并初始化 main</span><br>本地：'+esc(local);}
    }catch(e){/* toast already shown */}
  },350);
}

function showCreate(){$("createPanel").style.display="block";$("batchPanel").style.display="none"}
function hideCreate(){$("createPanel").style.display="none"}
function showBatch(){$("batchPanel").style.display="block";$("createPanel").style.display="none"}
function hideBatch(){$("batchPanel").style.display="none"}

async function createJob(){
  const explicit=$("c_baseline").value.trim();
  const repo=$("c_repo").value.trim();
  if(!explicit && !repo){toast("请填写 GitHub 仓库名（或在高级选项里指定已有本地仓库）",true);return;}
  const body={prompt:$("c_prompt").value,task_type:$("c_task_type").value,difficulty:$("c_difficulty").value,
   stack:$("c_stack").value,repro_level:$("c_repro").value,name:$("c_name").value,
   github_repo:explicit?"":repo,github_readme:$("c_readme").value,
   github_private:$("c_private").value==="1",baseline_parent_dir:$("c_parent").value,
   baseline_repo:explicit,check_commands:$("c_checks").value};
  const btn=event.target;btn.disabled=true;btn.textContent="创建中（建仓 + 初始化 main + 复制 A/B）…";
  try{
    const x=await api("/api/job_create",body);
    $("c_prompt").value="";$("c_repo").value="";$("c_readme").value="";$("c_baseline").value="";repoCheck();
    hideCreate();
    if(x.prepare_error){
      toast("已建仓但准备失败："+x.prepare_error,true);
    }else{
      const prov=x.provisioned;
      toast(prov
        ?`已创建：GitHub ${prov.created?"新建":"复用"} ${esc(prov.github.owner)}/${esc(prov.github.name)}${prov.seeded?"（已初始化 main）":""}，A/B 自动开跑`
        :"已创建并开始准备");
    }
    loadJobs();
  }finally{btn.disabled=false;btn.textContent="创建：建 GitHub 空仓 + 初始化 main → 复制 A/B 工作区 → 自动开跑";}
}
async function createBatch(){
  const body={lines:$("b_lines").value,task_type:$("b_task_type").value,difficulty:$("b_difficulty").value,
   stack:$("b_stack").value,baseline_parent_dir:$("b_parent").value};
  const x=await api("/api/job_batch",body);
  toast(`已创建 ${x.created.length} 条，准备完成 ${x.prepared.length} 条`+(x.errors.length?`，${x.errors.length} 条异常见详情`:""));
  $("b_lines").value="";hideBatch();loadJobs();
  if(x.errors.length)alert(x.errors.join("\n"));
}

async function loadJobs(isPoll){
  if(jobsInflight)return;
  jobsInflight=true;
  try{
    JOBS=attachChecks((await api("/api/jobs")).jobs);renderRows();portHubRefresh();if(SEL)renderDetail(SEL,false,isPoll)
  }finally{jobsInflight=false}
}
function attachChecks(jobs){
  // /api/jobs may omit check; keep the last report so polling cannot blank the
  // export button a few seconds after 「刷新检查」. Drop the overlay when the
  // evidence fingerprint changed, so we don't keep a stale red/green.
  return (jobs||[]).map(j=>{
    const slot=CHECKS[j.id];
    const c=j.check||(slot&&slot.fp===j.check_fp?slot.check:null);
    if(c){CHECKS[j.id]={check:c,fp:j.check_fp};return Object.assign({},j,{check:c})}
    if(slot)delete CHECKS[j.id];
    return j;
  });
}
function jobCheck(j){
  const slot=j&&CHECKS[j.id];
  const c=(j&&j.check)||(slot&&slot.fp===j.check_fp&&slot.check);
  return c||{items:[],ready:false,blocking_count:0,warning_count:0};
}
function isArchived(j){return !!j.archived}
function setView(v){JOB_VIEW=v;
  $("tabActive").classList.toggle("active",v==="active");
  $("tabArch").classList.toggle("active",v==="archived");
  renderRows();
}
function jobMatchesFilter(j,q){
  if(!q)return true;
  const hay=[j.name,j.task_type,j.difficulty,j.stack,j.github_repo,j.id].filter(Boolean).join(" ").toLowerCase();
  return hay.includes(q);
}
function visibleJobs(){
  const q=($("jobFilter").value||"").trim().toLowerCase();
  return JOBS.filter(j=>isArchived(j)===(JOB_VIEW==="archived")&&jobMatchesFilter(j,q));
}
function renderRows(){
  const active=JOBS.filter(j=>!isArchived(j)).length;
  const arch=JOBS.length-active;
  $("cntActive").textContent=active; $("cntArch").textContent=arch;
  const rows=visibleJobs();
  $("jobRows").innerHTML=rows.map(j=>{const a=j.sides.A,b=j.sides.B;
   const ev=[a.jsonl_local,b.jsonl_local,a.video_url||a.video_local,b.video_url||b.video_local].filter(Boolean).length;
   const rv=j.review||{};
   const gsb=rv.locked_at
     ? `<span class="ok">✓ 已标注</span>${rv.conclusion?` <span class="kv">${esc(rv.conclusion)}</span>`:""}`
     : (rv.conclusion
        ? `${esc(rv.conclusion)} <span class="warn" title="已选结论但尚未生成 TSV/锁定">未提交</span>`
        : '<span class="muted">待标注</span>');
   const busy=Object.values(j.sides).some(x=>x.live||["running","preparing","collecting"].includes(x.status));
   const arcBtn=isArchived(j)
     ? `<button class="ghost" title="恢复到待办队列" onclick="archiveJob('${j.id}',false)">↩ 恢复</button>`
     : `<button class="ghost" title="归档后从默认队列隐藏，不删除" ${busy?"disabled":""}
              onclick="archiveJob('${j.id}',true)">📦 归档</button>`;
   const statusTag=isArchived(j)
     ? `<span class="tag t-archived">已归档</span>`
     : `<span class="tag t-${j.status}">${stName(j.status)}</span>`;
   return `<tr class="${isArchived(j)?"rowarch":""}"><td>${statusTag}</td>
   <td><a href="#" onclick="openJob('${j.id}');return false">${esc(j.name)}</a><div class="kv">${esc(j.task_type)} · ${esc(j.difficulty)} · ${esc(j.stack)}</div></td>
   <td>${esc(j.task_type)}<br><span class="kv">${esc(j.difficulty)}</span></td>
   <td>${sideStatus(a.status, a.live)}<div class="kv">${esc((a.session_id||"").slice(0,8))}</div></td>
   <td>${sideStatus(b.status, b.live)}<div class="kv">${esc((b.session_id||"").slice(0,8))}</div></td>
   <td>${ev}/4</td><td>${gsb}</td>
   <td style="white-space:nowrap">${arcBtn}
   <button class="ghost" onclick="openJob('${j.id}')">打开</button></td></tr>`}).join("")
   || `<tr><td colspan="8" class="muted">${JOB_VIEW==="archived"?"还没有已归档任务":"没有匹配的任务（可切换到「已归档」或修改搜索）"}</td></tr>`;
}
async function archiveJob(id,on){
  try{
    await api("/api/job_action",{job:id,action:on?"archive":"unarchive"});
    if(on&&SEL===id)closeDetail();
    await loadJobs();
    toast(on?"已归档（可在「已归档」中恢复）":"已恢复到待办队列");
  }catch(e){/* toast already shown */}
}
function stName(s){return {draft:"草稿",ready:"待运行",running:"运行中",evidence_ready:"待标注",failed:"有失败",done:"已完成"}[s]||s}

function renderDetail(id,scroll,isPoll){const prevSel=SEL;
  const fid=document.activeElement?document.activeElement.id:"";
  const formIds=["r_validity","r_conclusion","r_reason","r_a_delivery_score","r_a_delivery_description","r_b_delivery_score","r_b_delivery_description","r_aic","vA","vB"];
  const detailOpen=$("detail").style.display==="block";
  // Polling must not rebuild the detail DOM while the operator is editing the
  // GSB form (an IME composing pinyin would be cancelled) or pasting a path.
  // Job data is still refreshed in JOBS; the DOM catches up on the next render.
  if(isPoll&&detailOpen&&prevSel===id&&formIds.includes(fid)){SEL=id;return}
  SEL=id;const j=JOBS.find(x=>x.id===id);if(!j)return;
  const wasSame=detailOpen&&prevSel===id;
  if(!wasSame){recActive={A:false,B:false};recPrimed={A:false,B:false};vDirty.A=vDirty.B=false;recWinSel.A=recWinSel.B="";["A","B"].forEach(s=>clearInterval(recTimers[s]))}
  if(wasSame)captureDetailDraft();
  const D=$("detail");D.style.display="block";
  const locked=!!(j.review&&j.review.locked_at);
  const c=jobCheck(j);
  D.innerHTML=`<div class="card">
    <div class="btns" style="margin-top:0"><button class="ghost" onclick="closeDetail()">← 返回列表</button>
    <span style="font-weight:700;font-size:15px;align-self:center">${esc(j.name)}</span><span class="sp" style="flex:1"></span>
    ${locked?'<span class="tag" style="background:#fef3c7" title="后端拒绝重跑/重新准备/重新采集">🔒 评审已锁定</span>':""}
    ${j.archived?'<span class="tag t-archived" title="已从默认队列隐藏，可恢复">📦 已归档</span>':""}
    <span class="tag t-${j.status}">${stName(j.status)}</span>
    ${j.archived
      ? '<button class="ghost" onclick="archiveJob(\''+j.id+'\',false)">↩ 恢复到待办</button>'
      : '<button class="ghost" title="归档后从默认队列隐藏，不删除证据" onclick="archiveJob(\''+j.id+'\',true)">📦 归档</button>'}
    </div>
    <div class="kv" style="margin:6px 0">${j.github_url?`仓库：<a href="${esc(j.github_url)}" target="_blank" class="mono">${esc(j.github_repo||j.github_url)}</a>${j.github_created===true?"（本次新建）":""} · `:""}基线：${j.baseline_url?`<a href="${esc(j.baseline_url)}" target="_blank" class="mono">${esc(j.baseline_sha.slice(0,12))}</a>`:"未准备"} · ${esc(j.harness)} ${esc(j.harness_version)} · ${esc(j.os_name)}</div>
    <div class="grid">
      ${sideHtml(j,"A")}${sideHtml(j,"B")}
    </div>
    <div class="btns">
      <button onclick="act('prepare')" ${locked?"disabled title='评审已锁定，请先在 GSB 区解锁'":""}>① 重新准备/校验基线</button>
      <button class="sec" onclick="act('enqueue')" ${locked?"disabled":""}>② 开始/重试运行</button>
      <button class="ghost" onclick="refreshDetail()">↻ 刷新检查</button>
      <button class="ghost" onclick="act('collect')" ${locked?"disabled":""}>重新采集会话</button>
      <button class="ghost" onclick="followOnly=null;startFollow()">📡 同时跟随 A/B</button>
      <button class="ghost" style="margin-left:auto;color:#b91c1c" onclick="deleteJob()">删除任务（清理工作区和证据）</button>
    </div>
    <div class="grid" style="margin-top:8px">
      <div class="sidebox"><h3>🎥 A 侧录屏</h3>
        <label style="font-weight:400">录制范围
          <select id="recWinA" onchange="recWinSel.A=this.value"><option value="">全屏：终端 + Web 切换全过程（推荐，真实验收）</option></select>
        </label>
        <div id="recA" class="kv ${j.sides.A.video_url||j.sides.A.video_local?'ok':'muted'}">${recSavedHtml(j,'A')}</div>
        <div class="btns" style="margin-top:6px">
          <button onclick="recStart('A')">● 开始录屏</button>
          <button class="sec" id="recStopA" onclick="recStop('A')" disabled>■ 运行结束，停止</button>
          <button class="ghost" type="button" onclick="pickVideo('A')">选择已有文件…</button>
        </div>
        <input id="vA" oninput="vDirty.A=true" value="${esc(j.sides.A.video_local||"")}" style="margin-top:6px" placeholder="也可直接粘贴 mp4 路径">
      </div>
      <div class="sidebox"><h3>🎥 B 侧录屏</h3>
        <label style="font-weight:400">录制范围
          <select id="recWinB" onchange="recWinSel.B=this.value"><option value="">全屏：终端 + Web 切换全过程（推荐，真实验收）</option></select>
        </label>
        <div id="recB" class="kv ${j.sides.B.video_url||j.sides.B.video_local?'ok':'muted'}">${recSavedHtml(j,'B')}</div>
        <div class="btns" style="margin-top:6px">
          <button onclick="recStart('B')">● 开始录屏</button>
          <button class="sec" id="recStopB" onclick="recStop('B')" disabled>■ 运行结束，停止</button>
          <button class="ghost" type="button" onclick="pickVideo('B')">选择已有文件…</button>
        </div>
        <input id="vB" oninput="vDirty.B=true" value="${esc(j.sides.B.video_local||"")}" style="margin-top:6px" placeholder="也可直接粘贴 mp4 路径">
      </div>
    </div>
    <p class="kv muted" style="margin:6px 2px">录制<b>真实运行</b>：建议全屏，从干净状态启动产物，终端命令和浏览器操作都会入镜；<b>产物运行结束立即点停止</b>，几秒即可，最长 89 秒会自动停止；失败的产物也要录。也可只录单个窗口，或绑定外部录好的 mp4。</p>
    <div class="btns">
      <button class="sec" onclick="act('set_videos')">保存录屏路径</button>
      <button onclick="act('upload')">③ 上传轨迹+录屏到 OSS</button>
    </div>
  </div>

  <div class="card" id="portCard">
    <h3 style="margin-top:0">⚠ 验收端口助手
      <span class="muted" style="font-weight:400"> · 勿把 Steam CEF / Inspectable WebContents 当成产物</span></h3>
    <p class="kv" style="margin:0 0 8px">Windows 上 <b>127.0.0.1:8080</b> 经常被 Steam 或<strong>上一题预览</strong>占用。点「空闲端口预览」只会打开<b>本题刚绑定的新端口</b>，并先关掉其他任务的预览。终端出现 <span class="mono">EADDRINUSE</span> 时，<b>禁止再打开已被占用的地址</b>，也不要用 <span class="mono">file://</span> 打开含 ES Module 的 index.html。推荐命令已避开 8080/5173 等常见占用位。</p>
    <div id="portScan" class="kv muted">打开任务后自动扫描…</div>
    <div class="grid" style="margin-top:8px">
      <div>
        <label style="font-weight:400">A 侧推荐启动（已避开占用端口）</label>
        <pre class="mono" id="portCmdA" style="white-space:pre-wrap;background:#f8fafc;border:1px solid #e2e8f0;padding:8px;border-radius:6px;min-height:2.6em;margin:0"></pre>
        <div class="btns">
          <button class="ghost" type="button" onclick="copyPortCmd('A')">复制 A 命令</button>
          <button class="sec" type="button" onclick="previewStart('A')">A 空闲端口预览</button>
          <button class="ghost" type="button" onclick="openWorkspace('A')">打开 A 工作区</button>
          <button type="button" onclick="openShell('A')">A 打开 PowerShell</button>
        </div>
      </div>
      <div>
        <label style="font-weight:400">B 侧推荐启动（已避开占用端口）</label>
        <pre class="mono" id="portCmdB" style="white-space:pre-wrap;background:#f8fafc;border:1px solid #e2e8f0;padding:8px;border-radius:6px;min-height:2.6em;margin:0"></pre>
        <div class="btns">
          <button class="ghost" type="button" onclick="copyPortCmd('B')">复制 B 命令</button>
          <button class="sec" type="button" onclick="previewStart('B')">B 空闲端口预览</button>
          <button class="ghost" type="button" onclick="openWorkspace('B')">打开 B 工作区</button>
          <button type="button" onclick="openShell('B')">B 打开 PowerShell</button>
        </div>
      </div>
    </div>
    <div class="btns">
      <button class="ghost" type="button" onclick="portScan()">重新扫描端口</button>
      <input id="probeUrl" placeholder="http://127.0.0.1:8080/" style="max-width:280px;width:auto;flex:1">
      <button class="ghost" type="button" onclick="portProbe()">探测该地址是不是产物</button>
      <button class="ghost" type="button" onclick="previewStop()">停止预览并清理 8080 残留</button>
    </div>
    <div id="portProbeOut" class="kv" style="margin-top:6px"></div>
  </div>

  <div class="card"><h3 style="margin-top:0">完整度检查 ${c.ready?'<span class="ok">✓ 可导出</span>':`（阻塞 ${c.blocking_count} / 提醒 ${c.warning_count}）`}</h3>
    <div id="checks">${checksHtml(c.items)}</div>
    <button class="ghost" onclick="refreshDetail(true)" style="margin-top:8px">在线核验链接可访问性（较慢）</button>
  </div>

  <details class="card" id="promptCard" open style="margin-bottom:12px">
    <summary style="cursor:pointer;font-weight:700;user-select:none">📝 User Prompt 完整原文（A/B 共用）<span class="muted" style="font-weight:400"> · 写 GSB 时对照题目要求，点击折叠</span></summary>
    <pre style="white-space:pre-wrap;word-break:break-word;margin:10px 2px 2px;max-height:240px;overflow:auto;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px 12px;font:12.5px/1.7 Consolas,'Microsoft YaHei',monospace;color:#334155">${esc(j.prompt||"(无提示词)")}</pre>
  </details>

  <div class="card"><h3 style="margin-top:0">④ GSB 人工判断（严禁 AI 代写）
    ${j.review.locked_at?'<span class="tag" style="background:#fef3c7">🔒 已锁定评审</span>':""}</h3>
    ${j.review.locked_at?`<p class="kv" style="color:#b45309">本任务已锁定：后端拒绝任何重跑/中止/重新准备/重新采集，确保证据在人工判断后不再变化。
    锁定时间 ${esc(j.review.locked_at)}${j.review.ai_confirmed_at?" · 未用 AI 确认于 "+esc(j.review.ai_confirmed_at):""}。</p>`:""}
    <div class="grid">
      <label>有效性<select id="r_validity" ${j.review.locked_at?"disabled":""}><option value="">请选择</option>${__VALIDITY__.map(x=>`<option ${x===j.review.validity?"selected":""}>${x}</option>`).join("")}</select></label>
      <label>结论<select id="r_conclusion" ${j.review.locked_at?"disabled":""}><option value="">请选择</option>${__CONCLUSIONS__.map(x=>`<option ${x===j.review.conclusion?"selected":""}>${x}</option>`).join("")}</select></label>
    </div>
    <label>GSB 理由（A、B 分别说明；Same 至少 80 字，其余 30 字以上；作废时可简述原因）<textarea id="r_reason" ${j.review.locked_at?"disabled":""} style="min-height:140px">${esc(j.review.reason||"")}</textarea></label>
    <p class="kv">交付完整性：分别评价两次 rollout 的产物质量和缺陷，也可从结果看题目难度。请写出具体不足；与 GSB 理由重复可以。旧任务可留空。</p>
    <div class="grid">
      ${["A","B"].map(s=>{const p=s.toLowerCase();return `<div>
        <label>${s} - 交付完整性（1-5）<select id="r_${p}_delivery_score" ${j.review.locked_at?"disabled":""}><option value="">请选择</option>${[1,2,3,4,5].map(n=>`<option value="${n}" ${String(j.review[p+"_delivery_score"]||"")===String(n)?"selected":""}>${n}</option>`).join("")}</select></label>
        <label>${s} - 交付完整性描述<textarea id="r_${p}_delivery_description" ${j.review.locked_at?"disabled":""} placeholder="说明产物质量、具体缺陷及题目难度体现">${esc(j.review[p+"_delivery_description"]||"")}</textarea></label>
      </div>`}).join("")}
    </div>
    <label style="font-weight:400;display:flex;gap:8px;align-items:flex-start">
      <input type="checkbox" id="r_aic" style="width:auto;margin-top:3px" ${j.review.ai_confirmed?"checked":""} ${j.review.locked_at?"disabled":""}>
      <span>我确认：以上结论与理由由我本人基于真实运行/代码/录屏独立判断完成，<b>未使用任何 AI（Claude/ChatGPT/Codex 等）分析轨迹、产物或代写理由</b>。</span>
    </label>
    <div class="btns">
      ${j.review.locked_at
        ? '<button class="ghost" onclick="unlockReview()">🔓 解锁（确认需要修改证据/重跑时）</button>'
        : '<button class="sec" onclick="saveReview(true)">保存 GSB 并锁定评审</button><button class="ghost" onclick="saveReview(false)">仅保存（不锁定）</button>'}
      <button class="sec" onclick="exportRow()" ${c.ready?"":"disabled"}>⑤ ${j.review.locked_at?"重新生成 TSV（已标注）":"生成 TSV 并标记已标注"}（阻塞项通过后可用）</button>
    </div>
    <div id="tsvBox" style="display:none;margin-top:10px">
      <pre class="log" id="tsvPre"></pre>
      <div class="btns"><button onclick="copyTsv()">复制 TSV（粘贴到飞书表格）</button>
      <button class="ghost" onclick="downloadTsv()">下载 .tsv 文件</button></div>
    </div>
  </div>`;
  loadRecWindows();
  if(!wasSame){recRefresh("A");recRefresh("B");portScan()}
  else{["A","B"].forEach(s=>{if(recActive[s])recRefresh(s)});restoreDetailDraft();if(lastPortScan)renderPortScan(lastPortScan)}
  if(!isPoll&&scroll!==false)D.scrollIntoView({behavior:"smooth"});
}
function sideHtml(j,s){const x=j.sides[s];const run=sideBusy(x);
  const locked=!!(j.review&&j.review.locked_at);
  return `<div class="sidebox"><h3>${s} 侧 ${sideStatus(x.status, x.live)}</h3>
  <div class="kv">工作区：${esc(x.workspace||"未准备")}<br>分支：${esc(x.branch)}<br>
  会话：${esc(x.session_id||"-")}<br>
  产物：${x.head_url?`<a href="${esc(x.head_url)}" target="_blank" class="mono">${esc(x.head_sha.slice(0,12))}</a>${x.pushed?" ✓push":" ✗未push"}`:"-"}<br>
  轨迹：${x.trace_url?`<a href="${esc(x.trace_url)}" target="_blank">链接</a>`:(x.jsonl_local?esc(x.jsonl_local.split("\\").pop()):"-")}<br>
  录屏：${x.video_url?`<a href="${esc(x.video_url)}" target="_blank">链接</a>`:(x.video_local?esc(x.video_local.split("\\").pop()):"缺失")}<br>
  ${(x.attempts&&x.attempts.length)?`尝试：${x.attempts.length} 次（证据目录 attempts/ 已逐次留存）<span title="${esc((x.attempts||[]).map(a=>"#"+a.attempt+" "+a.status+(a.failure?"："+a.failure:"")).join("\n"))}">ⓘ</span><br>`:""}
  ${x.error?`<span class="bad">${esc(x.error)}</span>`:""}</div>
  <div class="btns"><button class="ghost" ${run||locked?"disabled":""} title="${locked?"评审已锁定，请先在 GSB 区解锁":run?"运行中不能重跑，请先中止":""}" onclick="sideAct('retry','${s}')">重跑该侧</button>
  <button class="ghost" ${run?"":"disabled"} onclick="sideAct('abort','${s}')">中止</button>
  <button class="ghost" onclick="openLog('${s}')">运行日志</button>
  <button class="ghost" onclick="followSide('${s}')">📡 实时跟随</button>
  <button class="ghost" onclick="openShell('${s}')">PowerShell</button></div></div>`}
function checksHtml(items){if(!items||!items.length)return '<span class="muted">点「刷新检查」</span>';
  const groups={};items.forEach(i=>{(groups[i.group]=groups[i.group]||[]).push(i)});
  return Object.entries(groups).map(([g,xs])=>`<div style="margin:6px 0"><b>${esc(g)}</b><br>${xs.map(x=>
   `<span title="${esc(x.detail)}"><span class="dot ${x.ok?"g":x.blocking?"r":"y"}"></span><span class="${x.ok?"ok":x.blocking?"bad":"warn"}">${esc(x.label)}</span></span>`).join("　")}</div>`).join("")}
function closeDetail(){$("detail").style.display="none";SEL=null}
async function openJob(id){
  SEL=id;
  const j=JOBS.find(x=>x.id===id);
  const slot=CHECKS[id];
  const fresh=j&&(j.check||(slot&&slot.fp===j.check_fp));
  if(fresh){renderDetail(id);return}
  await refreshDetail();
}
async function refreshDetail(online){
  const id=SEL;if(!id)return;
  const j=await api("/api/job",{id:id,online:!!online});
  if(SEL!==id)return;
  if(j.check)CHECKS[j.id]={check:j.check,fp:j.check_fp};
  const f=JOBS.findIndex(x=>x.id===id);
  if(f>=0)JOBS[f]=Object.assign({},JOBS[f],j);
  else JOBS.push(j);
  renderRows();renderDetail(id,false)
}
async function act(a){const body={job:SEL,action:a,video_a:$("vA")? $("vA").value:"",video_b:$("vB")?$("vB").value:""};
  await api("/api/job_action",body);if(a==="delete"){delete CHECKS[SEL];closeDetail();await loadJobs();return}
  if(a==="set_videos")vDirty.A=vDirty.B=false; // paths are now the persisted server values
  await refreshDetail()}
function deleteJob(){
  const j=JOBS.find(x=>x.id===SEL);if(!j)return;
  const repo=j.github_repo||"",created=j.github_created===true;
  const pushed=["A","B"].some(s=>j.sides[s]&&j.sides[s].pushed);
  $("delWarn").innerHTML=`将清理本地 A/B 工作区、证据文件和任务记录，<b>不可恢复</b>。`
    +(repo?`<br>关联仓库：<span class="mono">${esc(repo)}</span>${created?"（交付台自动创建）":""}`:"<br>该任务未关联 GitHub 仓库。");
  const opts=[["keep","仅删除本地（远端 GitHub 内容保留）"]];
  if(repo&&pushed)opts.push(["branches","同时删除远端 A/B 分支（仓库保留）"]);
  if(repo&&created)opts.push(["repo",`同时删除整个 GitHub 仓库（含全部分支与本地基线文件夹）`]);
  $("delOpts").innerHTML=opts.map(([v,t],i)=>
    `<label style="font-weight:400;display:block;margin:6px 0"><input type="radio" name="delRemote" value="${v}" ${i===0?"checked":""} style="width:auto;margin-right:6px">${esc(t)}</label>`).join("");
  $("delModal").style.display="block";
}
async function doDelete(){
  const v=(document.querySelector('input[name="delRemote"]:checked')||{}).value||"keep";
  $("delModal").style.display="none";
  const id=SEL;
  const x=await api("/api/job_action",{job:id,action:"delete",remote:v});
  delete CHECKS[id];
  closeDetail();await loadJobs();
  toast("任务已删除"+(x.repo_deleted?`，远端仓库 ${x.repo_deleted} 已删除`:x.branches_deleted?`，远端分支 ${x.branches_deleted.join("、")} 已删除`:""));
}
async function sideAct(a,s){await api("/api/side_action",{job:SEL,side:s,action:a});await refreshDetail()}
async function saveReview(lock){
  const aic=$("r_aic");
  if(lock && !aic.checked){toast("请先勾选「未使用任何 AI」确认框再锁定",true);return}
  await api("/api/review",{job:SEL,validity:$("r_validity").value,conclusion:$("r_conclusion").value,
    reason:$("r_reason").value,...deliveryFields(),ai_confirmed:aic.checked,lock:!!lock});
  toast(lock?"GSB 已保存并锁定（重跑/改证据已被后端拒绝）":"GSB 已保存");refreshDetail()
}
async function unlockReview(){
  if(!confirm("解锁后将允许重新准备/重跑，可能改变证据。确定要解锁吗？"))return;
  await api("/api/review",{job:SEL,unlock:true});toast("已解锁，可修改证据");refreshDetail()
}
async function exportRow(){
  const aic=$("r_aic");
  const cur=JOBS.find(x=>x.id===SEL);
  const locked=!!(cur&&cur.review&&cur.review.locked_at);
  // Generating the TSV is the "submit" action: persist + lock in one go so the
  // list flips to 已标注 and re-runs/evidence edits are refused until unlock.
  if(!locked){
    if(!aic||!aic.checked){toast("请先勾选底部「未使用任何 AI」确认框，再生成 TSV（会同时锁定评审）",true);return}
    await api("/api/review",{job:SEL,validity:$("r_validity").value,conclusion:$("r_conclusion").value,
      reason:$("r_reason").value,...deliveryFields(),ai_confirmed:true,lock:true});
  }
  const x=await api("/api/export",{job:SEL});
  $("tsvBox").style.display="block";$("tsvPre").textContent=x.tsv;window.__tsv=x.tsv;
  await refreshDetail();
  toast(locked?"已生成 TSV":"已生成 TSV，评审已锁定 → 列表显示「已标注」；需修改请点详情里的🔓解锁");
}
function deliveryFields(){return {
  a_delivery_score:$("r_a_delivery_score").value,
  a_delivery_description:$("r_a_delivery_description").value,
  b_delivery_score:$("r_b_delivery_score").value,
  b_delivery_description:$("r_b_delivery_description").value,
}}
function copyTsv(){navigator.clipboard.writeText(window.__tsv||"");toast("已复制一行数据（无表头），去飞书表格整行粘贴")}
function downloadTsv(){const b=new Blob([window.__tsv||""],{type:"text/tab-separated-values"});const a=document.createElement("a");a.href=URL.createObjectURL(b);a.download=SEL+".tsv";a.click()}
async function openLog(s){const x=await api("/api/log",{job:SEL,side:s});const w=window.open("","_blank");w.document.write(`<pre style="font:12px Consolas;white-space:pre-wrap">${esc(x.log||"(空)")}</pre>`)}

// ---- live log following (read-only tail of run.log) ----
let followTimer=null, followOff={A:0,B:0}, followOnly=null;
function followSide(s){followOnly=s;startFollow()}
function startFollow(){
  followOff={A:0,B:0};["A","B"].forEach(s=>$("followLog"+s).textContent="");
  $("followTitle").textContent=SEL+(followOnly?` · 只看 ${followOnly} 侧`:" · A/B 双侧");
  if(followOnly){$("followLog"+followOnly).closest("div").style.gridColumn="1 / -1";["A","B"].filter(x=>x!==followOnly).forEach(x=>$("followLog"+x).closest("div").style.display="none")}
  $("follow").style.display="block";
  clearInterval(followTimer);tickFollow();followTimer=setInterval(tickFollow,2000);
}
async function tickFollow(){
  if(!SEL)return;
  const j=await api("/api/job",{id:SEL}).catch(()=>null);
  for(const s of ["A","B"]){
    if(followOnly&&s!==followOnly)continue;
    if(j&&j.sides&&j.sides[s])$("followState"+s).innerHTML="· "+sideStatus(j.sides[s].status,j.sides[s].live);
    let x;try{x=await fetch("/api/log_tail",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({job:SEL,side:s,offset:followOff[s]})}).then(r=>r.json())}catch(e){continue}
    if(typeof x.offset!=="number")continue;
    if(x.offset<followOff[s]){$("followLog"+s).textContent="";followOff[s]=0} // rotated
    if(x.text){const el=$("followLog"+s);const stick=$("followAutoscroll").checked&&(el.scrollTop+el.clientHeight>=el.scrollHeight-30);el.textContent+=x.text;if(stick)el.scrollTop=el.scrollHeight}
    followOff[s]=x.offset;
  }
}
function clearFollow(){["A","B"].forEach(s=>{followOff[s]=0;$("followLog"+s).textContent=""})}
function closeFollow(){clearInterval(followTimer);followTimer=null;followOnly=null;$("follow").style.display="none";["A","B"].forEach(s=>{const c=$("followLog"+s).closest("div");c.style.display="";c.style.gridColumn=""})}
async function pickVideo(s){const x=await api("/api/pick_file",{side:s});if(x.path){$("v"+s).value=x.path;vDirty[s]=true}}

// ---- preserve in-progress GSB draft / TSV box across polling re-renders ----
let detailDraft=null;
// vDirty marks a video path input the operator manually edited but has not
// persisted yet. Server-written paths (recStop/set_videos) must NOT be reverted
// by a draft restore, so only a dirty input is carried across re-renders.
const vDirty={A:false,B:false};
function captureDetailDraft(){
  const reason=$("r_reason");
  const tsv=$("tsvBox");
  const pc=$("promptCard");
  const pp=pc?pc.querySelector("pre"):null;
  let ae=null;
  if(document.activeElement&&["r_validity","r_conclusion","r_reason","r_a_delivery_score","r_a_delivery_description","r_b_delivery_score","r_b_delivery_description","r_aic","vA","vB"].includes(document.activeElement.id))
    ae={id:document.activeElement.id,start:null,end:null};
  if(ae&&["r_reason","r_a_delivery_description","r_b_delivery_description"].includes(ae.id)){const field=$(ae.id);ae.start=field.selectionStart;ae.end=field.selectionEnd}
  detailDraft={
    validity:$("r_validity")?$("r_validity").value:"",
    conclusion:$("r_conclusion")?$("r_conclusion").value:"",
    reason:reason?reason.value:"",
    ...deliveryFields(),
    aic:$("r_aic")?$("r_aic").checked:false,
    vA:$("vA")?$("vA").value:"",
    vB:$("vB")?$("vB").value:"",
    vADirty:vDirty.A,
    vBDirty:vDirty.B,
    tsvShown:tsv?tsv.style.display==="block":false,
    tsv:window.__tsv||"",
    promptOpen:pc?pc.open:true,
    promptScroll:pp?pp.scrollTop:0,
    focus:ae,
  };
}
function restoreDetailDraft(){
  if(!detailDraft)return;const d=detailDraft;
  const reason=$("r_reason");
  // Only restore when the draft differs from the last SAVED server state, i.e.
  // the operator has unsaved edits. Never clobber a freshly saved review.
  const cur=JOBS.find(x=>x.id===SEL);
  const saved=cur?cur.review:{};
  if(reason&&d.reason!==(saved.reason||"")){reason.value=d.reason}
  for(const key of ["a_delivery_score","a_delivery_description","b_delivery_score","b_delivery_description"]){
    const field=$("r_"+key);if(field&&d[key]!==String(saved[key]||""))field.value=d[key];
  }
  const v=$("r_validity");if(v&&d.validity&&d.validity!==(saved.validity||""))v.value=d.validity;
  const c=$("r_conclusion");if(c&&d.conclusion&&d.conclusion!==(saved.conclusion||""))c.value=d.conclusion;
  const a=$("r_aic");if(a&&d.aic&&d.aic!==!!saved.ai_confirmed)a.checked=d.aic;
  const va=$("vA");if(va&&d.vADirty)va.value=d.vA;
  const vb=$("vB");if(vb&&d.vBDirty)vb.value=d.vB;
  vDirty.A=!!d.vADirty;vDirty.B=!!d.vBDirty;
  if(d.tsvShown){const t=$("tsvBox");if(t){t.style.display="block";const p=$("tsvPre");if(p)p.textContent=d.tsv;window.__tsv=d.tsv}}
  const pc=$("promptCard");
  if(pc){pc.open=d.promptOpen!==false;const pp=pc.querySelector("pre");if(pp)pp.scrollTop=d.promptScroll||0}
  if(d.focus){const f=$(d.focus.id);if(f&&!f.disabled){f.focus();
    if(d.focus.start!=null&&f.setSelectionRange){try{f.setSelectionRange(d.focus.start,d.focus.end)}catch(e){}}
  }}
  detailDraft=null;
}
function recSavedHtml(j,s){const x=j.sides[s];
  if(x.video_url)return `已上传：<a href="${esc(x.video_url)}" target="_blank">${esc((x.video_local||x.video_url).split("\\").pop().split("/").pop())}</a>`;
  if(x.video_local)return `✓ 已保存<br><span class="muted">${esc(x.video_local)}</span>`;
  return "未开始";
}

let lastPortScan=null;
function renderPortScan(x){
  lastPortScan=x; if(!x)return;
  const el=$("portScan"); if(!el)return;
  const warns=(x.warnings||[]).map(w=>`<div class="bad" style="margin:3px 0">⚠ ${esc(w.message)}</div>`).join("");
  const occ=(x.listeners||[]).map(l=>`${l.addr||"127.0.0.1"}:${l.port} ${l.name||""} (${l.class||"?"})`).join(" · ");
  el.innerHTML=(warns||'<span class="ok">✓ 常见开发端口没有发现 Steam/CEF 占用</span>')
    +(occ?`<div class="muted" style="margin-top:4px">当前监听：${esc(occ)}</div>`:"");
  ["A","B"].forEach(s=>{
    const box=$("portCmd"+s); if(!box)return;
    const side=(x.sides||{})[s]||{};
    const cmd=(side.rewritten_commands||[])[0]||"";
    const notes=(side.notes||[]).join("\n");
    const prev=side.preview&&side.preview.running?("预览已开 "+side.preview.url+"\n"):"";
    box.textContent=prev+(cmd?(cmd+"\n打开本题 "+(side.open_url||"")):"（该侧工作区尚无 README / index.html 启动提示）")
      +(notes?"\n"+notes:"");
  });
}
async function portScan(){
  if(!SEL)return;
  const el=$("portScan"); if(el)el.textContent="扫描中…";
  try{renderPortScan(await api("/api/ports_scan",{job:SEL}))}
  catch(e){if(el)el.innerHTML='<span class="bad">扫描失败</span>'}
}
async function copyPortCmd(s){
  const side=(lastPortScan&&lastPortScan.sides||{})[s]||{};
  const cmd=(side.rewritten_commands||[])[0]||"";
  if(!cmd){toast("没有可复制的启动命令",true);return}
  try{await navigator.clipboard.writeText(cmd);toast("已复制（空闲端口 "+(side.free_port||"")+"）："+cmd)}
  catch(e){toast("复制失败，请手动选中命令",true)}
}
async function portProbe(){
  const url=(($("probeUrl")||{}).value||"http://127.0.0.1:8080/").trim();
  const out=$("portProbeOut"); if(out)out.textContent="探测中…";
  try{
    const x=await api("/api/ports_probe",{url});
    const kind=x.kind==="cef_debugger"?"bad":(x.ok?"ok":"warn");
    const label={cef_debugger:"这是 CEF/Steam 调试页，不是本题产物",product:"看起来是普通网页（请再核对是不是本题 UI）",
      empty:"页面为空",unreachable:"连不上（可能没启动，或端口不对）",unknown:"无法判断"}[x.kind]||x.kind;
    if(out)out.innerHTML=`<span class="${kind}">${esc(label)}</span> · HTTP ${esc(x.status||"-")} · 标题 ${esc(x.title||"-")}<div class="muted">${esc(x.snippet||x.error||"")}</div>`;
    if(x.kind==="cef_debugger")toast("探测结果：Inspectable WebContents / Steam CEF，禁止当作产物",true);
  }catch(e){if(out)out.innerHTML='<span class="bad">探测失败</span>'}
}
async function previewStart(s){
  const x=await api("/api/preview_start",{job:SEL,side:s});
  const port=x.port||0;
  if([8080,5173,3000,8000].includes(port)){
    toast(s+" 侧预览落到了常见占用端口 :"+port+"，已拒绝打开。请点「停止全部预览并清理残留」后再试",true);
    portScan();portHubRefresh();return;
  }
  toast(s+" 侧已在本题空闲端口启动预览："+x.url+"（已关掉其他任务的预览）");
  if(x.url)window.open(x.url,"_blank");
  portScan();portHubRefresh();
}
async function previewStop(){
  await portsReapAll();
  portScan();
}
async function portHubRefresh(){
  const el=$("portHubBody"); if(!el)return;
  try{
    const x=await api("/api/ports_overview",{});
    const prev=(x.previews||[]).map(p=>`预览 ${esc(p.job_name||p.job)} ${esc(p.side)} → <a href="${esc(p.url)}" target="_blank">${esc(p.url)}</a> <span class="muted">${esc(p.workspace||"")}</span>`);
    const left=(x.leftovers||[]).map(l=>{
      const tag=l.killable?"残留可清":(l.class==="foreign"?"勿动（Steam/系统）":"占用");
      return `${esc(l.name||"")} :${l.port} pid ${l.pid} · ${tag}`;
    });
    if(!prev.length&&!left.length){
      el.innerHTML='<span class="ok">✓ 没有交付台预览，常见开发端口上也没有可清的 python/node 残留</span>';
      return;
    }
    el.innerHTML=(prev.length?`<div>${prev.join("<br>")}</div>`:"")
      +(left.length?`<div class="warn" style="margin-top:6px">⚠ ${left.join("<br>⚠ ")}</div>`:"");
  }catch(e){el.innerHTML='<span class="bad">端口总览扫描失败</span>'}
}
async function portsReapAll(){
  const x=await api("/api/ports_reap",{});
  const n=(x.killed||[]).length;
  toast(n?("已停全部预览并结束残留："+(x.killed||[]).map(k=>k.name+" :"+k.port).join("、")):"已停止全部任务的预览（8080 上没有可杀的外部 python/node）");
  portHubRefresh();
}
async function openWorkspace(s){
  await api("/api/open_workspace",{job:SEL,side:s});
  toast("已打开 "+s+" 侧工作区文件夹");
}
async function openShell(s){
  await api("/api/open_shell",{job:SEL,side:s});
  toast("已打开 "+s+" 侧 PowerShell（工作目录=该侧工作区）");
}

let recTimers={}, recActive={A:false,B:false}, recPrimed={A:false,B:false};
let recWindows=[], recWindowsLoaded=false; // cached enumerator window titles; survive re-renders
const recWinSel={A:"",B:""}; // last chosen window per side, restored after re-render
async function loadRecWindows(){
  const fill=()=>{
    ["A","B"].forEach(s=>{
      const sel=$("recWin"+s);if(!sel)return;
      (recWindows||[]).forEach(t=>{
        if(Array.from(sel.options).some(o=>o.value===t))return;
        const o=document.createElement("option");o.value=t;o.textContent="仅窗口："+t;sel.appendChild(o);
      });
      if(recWinSel[s])sel.value=recWinSel[s];
    });
  };
  // Re-renders rebuild the <select> with only the built-in fullscreen option,
  // so refill from the cache every time; fetch the enumerator list only once.
  if(recWindowsLoaded){fill();return}
  try{
    const x=await api("/api/rec_windows",{});
    recWindows=(x.windows||[]).map(w=>w.title);
    recWindowsLoaded=true;
    fill();
  }catch(e){/* full screen still works */}
}
async function recStart(s){
  const sel=$("recWin"+s), win=sel?sel.value:"";
  try{
    const scan=await api("/api/ports_scan",{job:SEL});
    lastPortScan=scan;renderPortScan(scan);
    if(scan.warnings&&scan.warnings.length){
      toast("⚠ "+scan.warnings[0].message,true);
    }
  }catch(e){/* scan is advisory */}
  await api("/api/rec_start",{job:SEL,side:s,window:win,max_seconds:SETTINGS.video_max_seconds||89,fps:SETTINGS.video_fps||15});
  toast(s+" 侧开始录屏——从干净状态真实运行产物（终端+浏览器），结束立即停止");recRefresh(s)
}
async function recStop(s){await api("/api/rec_stop",{job:SEL,side:s});await refreshDetail();toast(s+" 侧录屏已保存")}
async function recRefresh(s){
  clearInterval(recTimers[s]);
  const jobId=SEL;
  let x;try{const r=await fetch("/api/rec_status",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({job:jobId,side:s})});x=await r.json()}catch(e){return}
  const el=$("rec"+s);if(!el)return;
  const wasRec=recActive[s];recActive[s]=!!x.recording;
  if(x.recording){
    const btn=$("recStop"+s);if(btn)btn.disabled=false;
    const cap=x.max_seconds?` / ${x.max_seconds}s 自动停`:"";
    const html=`🔴 录制中 <b>${x.elapsed}s</b>${cap}${x.window?` · 窗口：${esc(x.window)}`:" · 全屏"}`;
    if(!wasRec||el.innerHTML!==html){el.innerHTML=html;el.className="kv bad"}
    recTimers[s]=setInterval(()=>recRefresh(s),1000);
  }else{
    const btn=$("recStop"+s);if(btn)btn.disabled=true;
    if(wasRec){
      if(x.path){el.innerHTML=`✓ 已保存 ${x.duration?x.duration.toFixed(0)+"s":""} ${x.size?(x.size/1024/1024).toFixed(1)+"MB":""}<br><span class="muted">${esc(x.path)}</span>`;el.className="kv ok"}
      else{
        el.textContent="未开始";el.className="kv muted";
        // Narrow window: ffmpeg exited but the watcher has not finalised the
        // file yet. Re-check shortly after instead of staying on "未开始".
        setTimeout(()=>{if(SEL===jobId&&!recActive[s]){clearInterval(recTimers[s]);recPrimed[s]=false;recRefresh(s)}},1200);
      }
      recPrimed[s]=true;
    }else if(!recPrimed[s]){
      // First status fetch for this detail view: enrich the server-persisted
      // frame once with the file's size/duration. Afterwards never rewrite it
      // (rewriting on every poll was the source of the visible flicker).
      if(x.path){el.innerHTML=`✓ 已保存 ${x.duration?x.duration.toFixed(0)+"s":""} ${x.size?(x.size/1024/1024).toFixed(1)+"MB":""}<br><span class="muted">${esc(x.path)}</span>`;el.className="kv ok"}
      recPrimed[s]=true;
    }
  }
}

loadDefaults().then(loadJobs);
pollTimer=setInterval(()=>{
  portHubRefresh();
  if(JOBS.some(j=>["running","ready"].includes(j.status)||Object.values(j.sides).some(x=>x.live||["running","preparing"].includes(x.status))))loadJobs(true);
},5000);
</script></body></html>"""


SETTINGS_PAGE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>后台设置 · Pair 交付台</title>
<style>
:root{--bg:#f4f5f8;--card:#fff;--line:#dde2ea;--ink:#1f2937;--muted:#6b7280;--blue:#2563eb;--bad:#b91c1c}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,"Segoe UI",sans-serif;background:var(--bg);color:var(--ink)}
header{background:#0f172a;color:#fff;padding:12px 20px;display:flex;align-items:center;gap:14px}
header h1{font-size:16px;margin:0}header .sp{flex:1}
.wrap{max-width:900px;margin:18px auto;padding:0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:14px 0}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
label{display:block;font-weight:600;margin:8px 0 3px;font-size:13px}
input{width:100%;padding:8px;border:1px solid #b8c2d1;border-radius:6px;font:inherit;background:#fff}
button{padding:8px 13px;border:0;border-radius:6px;background:var(--blue);color:#fff;cursor:pointer;font:inherit}
button.sec{background:#475569}button.ghost{background:#e5e7eb;color:#111}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.muted{color:var(--muted)}.bad{color:var(--bad)}.ok{color:#15803d}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.kv{font-size:12px;color:var(--muted);word-break:break-all}
.toast{position:fixed;right:18px;bottom:18px;background:#111827;color:#fff;padding:10px 14px;border-radius:8px;display:none;max-width:420px}
a{color:var(--blue)}.hint{background:#f8fafc;border:1px solid var(--line);border-radius:8px;padding:10px;margin:10px 0;font-size:13px}
</style></head><body>
<header><h1>Pair 交付台 · 后台设置</h1><span class="sp"></span><a href="/" style="color:#cbd5e1">← 返回标注台</a></header>
<div class="wrap">

<div class="card">
  <h2 style="margin-top:0">运行引擎</h2>
  <p class="muted" style="margin:0 0 6px">上游（seed-code 网关）深度思考编码轮经常静默断流或中途 504；遇到断流会自动放弃残缺轮、重新复制干净基线副本并重试，直到拿到完整轮次。并行 pair 数保存后立刻扩容工人（不必重启）；8 路 pair = 最多 16 个 claude，受供应商 Key 并发上限约束。</p>
  <div class="grid3">
    <label>claude 命令<input id="s_claude"></label>
    <label>最多并行 pair 数<input id="s_parallel" type="number" min="1" max="16"></label>
    <label>单侧单次超时（秒）<input id="s_timeout" type="number" min="300" max="14400"></label>
    <label>单侧总预算（秒，含重试）<input id="s_wall" type="number" min="60" max="28800"></label>
    <label title="0=等 Claude 自己退出或报错再重试，不因思考静默杀进程">断流判定（秒无活动，0=等进程报错）<input id="s_stall" type="number" min="0"></label>
    <label>单侧最多自动重试次数（最小 12）<input id="s_attempts" type="number" min="12" max="16"></label>
    <label>活动轮询间隔（秒）<input id="s_poll" type="number" min="5"></label>
    <label title="-p 非交互下 acceptEdits 会自动拒绝所有 Bash，模型会空转整轮；工作区是一次性副本，默认完全放行">
      运行权限模式
      <select id="s_perm">
        <option value="bypassPermissions">bypassPermissions（完全放行，推荐）</option>
        <option value="acceptEdits">acceptEdits（只放行文件编辑，Bash 会被拒）</option>
        <option value="dontAsk">dontAsk（拒绝需授权项，不弹询问）</option>
        <option value="plan">plan（只读规划，不执行）</option>
      </select>
    </label>
    <label title="真人运行产物的录屏上限；到点 ffmpeg 自动正常结束，几秒也可以">
      录屏自动停止（秒，建议 60–89）<input id="s_vidmax" type="number" min="5" max="600"></label>
    <label title="帧率越低文件越小；终端/网页演示 10–15 足够">录屏帧率 FPS<input id="s_vidfps" type="number" min="5" max="30"></label>
  </div>
</div>

<div class="card">
  <h2 style="margin-top:0">默认值</h2>
  <div class="grid3">
    <label style="grid-column:1/-1">基线父目录（新任务只填仓库名时，本地文件夹建在这里）
      <input id="s_parent" placeholder="D:\myprojects\GoletaLab数据标注\github-base"></label>
    <label>GitHub 归属账号（通常留空即可；填写则必须与 gh 登录账号一致）<input id="s_owner" placeholder="留空 = 当前登录账号"></label>
    <label>新仓库默认可见性<select id="s_private">
      <option value="0">公开</option>
      <option value="1">私有</option>
    </select></label>
    <label style="grid-column:1/-1;font-weight:400">
      <span class="muted">兼容旧流程：已有本地基线仓库时，在新建任务面板「高级」里直接填目录即可。</span>
      <input id="s_baseline" type="hidden"></label>
  </div>
  <div class="grid3">
    <label>TSV 提交人（飞书按账号标注，一般无需改）<input id="s_reviewer"></label>
  </div>
  <div id="ghStatus" class="hint" style="margin-top:10px"></div>
</div>

<div class="card">
  <h2 style="margin-top:0">京东云 OSS</h2>
  <div class="grid3">
    <label>OSS Endpoint<input id="s_endpoint"></label>
    <label>OSS 区域<input id="s_region"></label>
    <label>OSS Bucket<input id="s_bucket"></label>
    <label>公网访问基址（留空用 path-style）<input id="s_pubbase"></label>
    <label>对象 key 前缀<input id="s_prefix"></label>
  </div>
  <div class="hint">
    <b>京东云密钥</b>只放在 <span class="mono" id="secretPath"></span>，不进仓库。<br>
    文件内容两行：<span class="mono">OSS_ACCESS_KEY_ID=...</span> 和 <span class="mono">OSS_SECRET_ACCESS_KEY=...</span>
    <div class="kv" id="ossStatus" style="margin-top:6px"></div>
  </div>
  <div class="btns">
    <button class="sec" onclick="saveSettings()">保存设置</button>
    <button class="ghost" onclick="ossTest()">测试连接 / 列出 bucket</button>
    <button class="ghost" onclick="ossCreate()">创建 bucket（不存在时）</button>
  </div>
</div>

</div>
<div class="toast" id="toast"></div>
<script>
const $=id=>document.getElementById(id);
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function toast(m,bad){const t=$("toast");t.textContent=m;t.style.background=bad?"#7f1d1d":"#111827";t.style.display="block";setTimeout(()=>t.style.display="none",4000)}
async function api(path,body){
  const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:body?JSON.stringify(body):"{}"});
  const x=await r.json().catch(()=>({error:"bad json"}));if(!r.ok||x.ok===false){toast(x.error||"请求失败",true);throw x}return x}
let SETTINGS={};
async function load(){SETTINGS=await api("/api/settings_get");
  $("s_claude").value=SETTINGS.claude_command;$("s_parallel").value=SETTINGS.max_parallel_pairs;
  $("s_timeout").value=SETTINGS.side_timeout_seconds;$("s_wall").value=SETTINGS.side_wall_budget_seconds||14400;$("s_stall").value=SETTINGS.stall_seconds;
  $("s_attempts").value=Math.max(12,SETTINGS.side_max_attempts);$("s_poll").value=SETTINGS.activity_poll_seconds;
  $("s_perm").value=SETTINGS.permission_mode||"bypassPermissions";
  $("s_vidmax").value=SETTINGS.video_max_seconds||89;
  $("s_vidfps").value=SETTINGS.video_fps||15;
  $("s_endpoint").value=SETTINGS.oss_endpoint;$("s_region").value=SETTINGS.oss_region;
  $("s_bucket").value=SETTINGS.oss_bucket;$("s_pubbase").value=SETTINGS.oss_public_base||"";
  $("s_prefix").value=SETTINGS.oss_key_prefix;$("s_baseline").value=SETTINGS.default_baseline_repo||"";
  $("s_parent").value=SETTINGS.baseline_parent_dir||"";$("s_owner").value=SETTINGS.github_owner||"";
  $("s_private").value=SETTINGS.github_private?"1":"0";
  $("s_reviewer").value=SETTINGS.reviewer||"";$("secretPath").textContent=SETTINGS.secret_path;
  ghStatus();
  const st=SETTINGS.oss;$("ossStatus").innerHTML=st.configured
    ?`已配置：${esc(st.endpoint)} / bucket=<b>${esc(st.bucket)}</b> / key=${esc(st.access_key_id_preview)}`
    :`<span class="bad">未配置（endpoint/bucket 或密钥缺失）</span>`;
}
async function saveSettings(){const body={claude_command:$("s_claude").value,max_parallel_pairs:+$("s_parallel").value,
 side_timeout_seconds:+$("s_timeout").value,side_wall_budget_seconds:+$("s_wall").value,stall_seconds:+$("s_stall").value,
 side_max_attempts:Math.max(12,+$("s_attempts").value),activity_poll_seconds:+$("s_poll").value,
 permission_mode:$("s_perm").value,
 video_max_seconds:Math.max(5,+$("s_vidmax").value||89),video_fps:Math.max(5,+$("s_vidfps").value||15),
 oss_endpoint:$("s_endpoint").value,oss_region:$("s_region").value,
 oss_bucket:$("s_bucket").value,oss_public_base:$("s_pubbase").value,oss_key_prefix:$("s_prefix").value,
 default_baseline_repo:$("s_baseline").value,
 baseline_parent_dir:$("s_parent").value,github_owner:$("s_owner").value,
 github_private:$("s_private").value==="1",
 reviewer:$("s_reviewer").value};
 SETTINGS=await api("/api/settings_save",body);
 const n=SETTINGS.workers||SETTINGS.max_parallel_pairs||0;
 toast("设置已保存：现在 "+n+" 路 pair 并行（最多 "+(2*n)+" 个 claude），排队中的任务会马上被新工人领走，不必重启")}
async function ghStatus(){
  const el=$("ghStatus");if(!el)return;
  el.innerHTML='<span class="muted">检测 gh 登录状态…</span>';
  try{
    const x=await api("/api/gh_status",{});
    el.innerHTML=x.login
      ?`<span class="ok">✓ gh 已登录：<b>${esc(x.login)}</b>（新仓库将建在此账号下）</span>`
      :`<span class="bad">✗ ${esc(x.error||"gh 未登录")} — 请在终端执行 <code>gh auth login</code>（需 repo 权限）</span>`;
  }catch(e){el.innerHTML='<span class="bad">✗ 状态检测失败</span>';}
}
async function ossTest(){const x=await api("/api/oss_test",{endpoint:$("s_endpoint").value,region:$("s_region").value,bucket:$("s_bucket").value});
 toast(x.ok?("连接成功，bucket: "+(x.buckets||[]).join(", ")+(x.bucket_present?"（目标 bucket 存在）":"（目标 bucket 不存在，可点创建）")):("失败: "+x.error),!x.ok)}
async function ossCreate(){const x=await api("/api/oss_create",{endpoint:$("s_endpoint").value,region:$("s_region").value,bucket:$("s_bucket").value});
 toast(x.existed?"bucket 已存在":"bucket 已创建: "+x.name)}
load();
</script></body></html>"""


# Browser refresh / overlapping 5s polls abort the previous fetch. Windows
# surfaces that as WinError 10053/10054; it is not a desk crash.
_CLIENT_GONE = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError)
_WIN_CLIENT_GONE = {10053, 10054}  # WSAECONNABORTED / WSAECONNRESET


def _is_client_gone(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, _CLIENT_GONE):
        return True
    winerr = getattr(exc, "winerror", None)
    return winerr in _WIN_CLIENT_GONE


def _side_workspace(job: dict, side: str) -> Path:
    """Resolve the job's own A/B workspace. Never takes a client-supplied path."""
    name = str(side or "").upper()
    if name not in ("A", "B"):
        raise RuntimeError("side 必须是 A 或 B")
    wdir = (job.get("sides") or {}).get(name, {}).get("workspace") or ""
    path = Path(wdir)
    if not wdir or not path.is_dir():
        raise RuntimeError(f"{name} 侧工作区不存在")
    return path.resolve()


def _powershell_exe() -> str:
    import os
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    bundled = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(bundled) if bundled.is_file() else "powershell.exe"


def _powershell_popen(wdir: Path, *, title: str = "") -> tuple[list[str], dict]:
    """Visible PowerShell at *wdir* (works even when the desk is pythonw.exe)."""
    ps_path = str(wdir).replace("'", "''")
    ps_title = (title or "工作区").replace("'", "''")
    args = [
        _powershell_exe(), "-NoExit", "-NoLogo",
        "-Command",
        "try { $Host.UI.RawUI.WindowTitle = '%s' } catch {}; Set-Location -LiteralPath '%s'"
        % (ps_title, ps_path),
    ]
    kwargs: dict[str, str | int] = {"cwd": str(wdir)}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
    return args, kwargs


class _ExclusiveServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets two processes bind the same port; refuse it so
    # a second desk fails loudly instead of silently double-running every job.
    allow_reuse_address = False
    daemon_threads = True

    def handle_error(self, request, client_address):
        if _is_client_gone(sys.exc_info()[1]):
            return
        super().handle_error(request, client_address)


class DeskServer:
    def __init__(self, store: DeskStore, port: int = 8765):
        self.store = store
        self.runner = PairRunner(store)
        self.recorder = Recorder(store)
        self.previews = ports_mod.PreviewRegistry()
        self.port = port
        self._lock_fp = None
        self._gh_login_cache: tuple[str, float] | None = None
        # Last computed completeness report per job. /api/jobs does not rerun
        # the checklist (git ancestry, ffprobe); without this overlay a 5s poll
        # replaces JOBS and the UI drops back to 「点刷新检查」.
        self._checklist_cache: dict[str, dict] = {}

    def _gh_login(self, *, ttl_seconds: float = 60.0) -> str:
        """Cached gh login to avoid spawning `gh api user` on every keystroke probe."""
        import time as _time
        now = _time.time()
        if self._gh_login_cache and now - self._gh_login_cache[1] < ttl_seconds:
            return self._gh_login_cache[0]
        login = ghutil.CliGh().viewer_login()  # raises GitHubError when logged out
        self._gh_login_cache = (login, now)
        return login

    def _acquire_singleton_lock(self) -> bool:
        """Cross-process exclusive lock; False when another desk owns it."""
        import sys
        lock_path = self.store.home / "desk.lock"
        fp = open(lock_path, "a+", encoding="utf-8")
        try:
            if sys.platform == "win32":
                import msvcrt
                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fp.seek(0)
            fp.truncate()
            fp.write(str(__import__("os").getpid()))
            fp.flush()
            self._lock_fp = fp
            return True
        except (OSError, ValueError):
            fp.close()
            return False

    def oss_cfg(self, overrides: dict | None = None) -> oss_mod.OssConfig | None:
        settings = dict(self.store.settings())
        if overrides:
            for key in ("endpoint", "region", "bucket"):
                val = overrides.get(key)
                if val:
                    settings[{"endpoint": "oss_endpoint", "region": "oss_region", "bucket": "oss_bucket"}[key]] = val
        return oss_mod.config_from_mapping(settings, self.store.secrets_path)

    # ---------- actions ----------
    REVIEW_LOCKED_MSG = "该任务已进入人工评审锁定状态（已确认未用 AI）。如需重跑/改证据，请先在 GSB 区「解锁」。"

    def _ensure_review_unlocked(self, job_id: str) -> None:
        if DeskStore.review_locked(self.store.get_job(job_id)):
            raise RuntimeError(self.REVIEW_LOCKED_MSG)

    def _ensure_not_archived(self, job_id: str) -> None:
        if self.store.get_job(job_id).get("archived"):
            raise RuntimeError("该任务已归档。请先在列表切换到「已归档」并点「恢复」，再执行此操作。")

    def _set_archived(self, job_id: str, archived: bool) -> dict:
        """Archive (hide) or restore a job. Running jobs cannot be archived."""
        job = self.store.get_job(job_id)
        if archived and any(self.runner.is_side_live(job_id, s) for s in ("A", "B")):
            raise RuntimeError("任务仍在运行/采集中，请先结束或等待完成后再归档")
        if archived:
            # Belt-and-braces: never archive while a run is queued/in-flight,
            # even if the live flag has not flipped yet.
            for side in job.get("sides", {}).values():
                if side.get("status") in ("running", "preparing", "collecting"):
                    raise RuntimeError("任务仍在运行/准备/采集中，不能归档")
        updated = self.store.set_archived(job_id, archived)
        return {"archived": bool(updated.get("archived")), "job": updated["id"]}

    def job_create(self, body: dict, prepare: bool = True) -> dict:
        job = self.store.create_job(body)
        result: dict = {"job": job["id"], "prepared": [], "provisioned": None}
        needs_baseline = bool(body.get("baseline_repo") or body.get("github_repo"))
        if prepare and needs_baseline:
            try:
                prep = self.runner.prepare(job["id"])
                result["provisioned"] = prep.get("provisioned")
                result["prepared"].append(job["id"])
                self.runner.enqueue(job["id"])
            except Exception as exc:
                self.store.update_job(job["id"], {"status": "failed", "error": str(exc)})
                result["prepare_error"] = str(exc)
        return result

    @staticmethod
    def _looks_like_local_path(value: str) -> bool:
        """Heuristic separating an existing-repo path from a bare GitHub repo name."""
        v = value.strip()
        if not v:
            return False
        if "\\" in v or "/" in v:
            return True
        # X: style drive prefix without backslashes is rare but cheap to catch.
        return len(v) >= 2 and v[1] == ":"

    def job_batch(self, body: dict) -> dict:
        import csv as _csv
        import io as _io
        created: list[str] = []
        prepared: list[str] = []
        errors: list[str] = []
        default_parent = clean_path(body.get("baseline_parent_dir", ""))
        for raw in _io.StringIO(body.get("lines", "")):
            line = raw.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            row = next(_csv.reader(_io.StringIO(line), delimiter="\t"))
            fifth = row[4].strip() if len(row) > 4 else ""
            is_path = self._looks_like_local_path(fifth)
            data = {
                "prompt": row[0].strip(),
                "task_type": (row[1] if len(row) > 1 and row[1].strip() else body.get("task_type")),
                "difficulty": (row[2] if len(row) > 2 and row[2].strip() else body.get("difficulty")),
                "stack": (row[3] if len(row) > 3 and row[3].strip() else body.get("stack")),
                # A path-like 5th column reuses an existing local repo; anything
                # else is treated as a GitHub repo name to auto-provision.
                "baseline_repo": fifth if is_path else "",
                "github_repo": "" if is_path else fifth,
                "baseline_parent_dir": default_parent,
            }
            if not data["prompt"] or not (data["baseline_repo"] or data["github_repo"]):
                errors.append(f"跳过（缺提示词或仓库名/目录）: {data['prompt'][:30]}")
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
            self._ensure_review_unlocked(job_id)
            self._ensure_not_archived(job_id)
            return {"result": self.runner.prepare(job_id)}
        if action == "enqueue":
            self._ensure_review_unlocked(job_id)
            self._ensure_not_archived(job_id)
            job = self.store.get_job(job_id)
            if not job.get("baseline_sha"):
                self.runner.prepare(job_id)
            for side_name, side in self.store.get_job(job_id)["sides"].items():
                if side["status"] == "failed" and not self.runner.is_side_running(job_id, side_name):
                    # Recopy every failed side first. enqueue=True here would
                    # launch the pair while the sibling is still dirty.
                    self.runner.retry_side(job_id, side_name, enqueue=False)
            self.runner.enqueue(job_id)
            self.runner._sync_job_status(job_id)
            return {"queued": True}
        if action == "collect":
            self._ensure_review_unlocked(job_id)
            self._ensure_not_archived(job_id)
            for side_name in ("A", "B"):
                self._recollect_side(job_id, side_name)
            return {"collected": True}
        if action == "archive":
            return self._set_archived(job_id, True)
        if action == "unarchive":
            return self._set_archived(job_id, False)
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
        if action == "delete":
            remote = str(body.get("remote") or "keep")
            if remote not in ("keep", "branches", "repo"):
                raise RuntimeError(f"未知的 remote 参数 {remote}")
            for side_name in ("A", "B"):
                try:
                    self.runner.abort_side(job_id, side_name)
                except Exception:
                    pass
                try:
                    self.previews.stop_side(job_id, side_name)
                except Exception:
                    pass
            job = self.store.get_job(job_id)
            # Remote cleanup runs BEFORE local state is removed: when it fails
            # the job record stays, so the user can retry or pick 仅删除本地.
            remote_result = self._delete_remote(job, remote) if remote != "keep" else {}
            for side_name in ("A", "B"):
                wdir = job["sides"][side_name].get("workspace", "")
                if wdir:
                    ws.robust_rmtree(wdir)
            pair_dir = self.store.workspaces / job_id
            if pair_dir.exists():
                ws.robust_rmtree(pair_dir)
            ev_dir = self.store.evidence / job_id
            if ev_dir.exists():
                ws.robust_rmtree(ev_dir)
            self.store.delete_job(job_id)
            self._checklist_cache.pop(job_id, None)
            if remote_result.get("baseline_dir"):
                # The repo is gone remotely; drop the auto-provisioned local
                # baseline folder too, unless another job still uses it.
                baseline = remote_result["baseline_dir"]
                still_used = any(
                    j.get("baseline_repo") == baseline
                    for j in self.store.list_jobs()
                )
                if not still_used and Path(baseline).is_dir():
                    ws.robust_rmtree(baseline)
            return {"deleted": True, **remote_result}
        raise RuntimeError(f"未知操作 {action}")

    def _delete_remote(self, job: dict, mode: str) -> dict:
        """Delete this job's GitHub artifacts (branches, or the whole repo).

        Raises RuntimeError on any failure so the caller keeps local state.
        """
        repo_full = (job.get("github_repo") or "").strip()
        if "/" not in repo_full:
            raise RuntimeError(
                "该任务没有记录 GitHub 仓库（可能是纯本地基线），没有可删的远端内容"
            )
        owner = repo_full.split("/", 1)[0]
        login = self._gh_login()
        if owner.lower() != login.lower():
            raise RuntimeError(
                f"仓库 {repo_full} 不属于当前 gh 登录账号 {login}，拒绝删除远端内容"
            )
        cli = ghutil.CliGh()
        if mode == "repo":
            if not job.get("github_created"):
                raise RuntimeError(
                    f"{repo_full} 不是交付台自动创建的仓库（可能是已有基线仓库），"
                    "只允许删除 A/B 分支，不允许删除整个仓库"
                )
            cli.repo_delete(repo_full)
            return {"repo_deleted": repo_full,
                    "baseline_dir": (job.get("baseline_repo") or "").strip()}
        branches = [
            job["sides"][s].get("branch", "")
            for s in ("A", "B")
            if job["sides"][s].get("pushed") and job["sides"][s].get("branch")
        ]
        if not branches:
            raise RuntimeError("该任务没有已 push 的远端分支，无需清理")
        errors = cli.delete_remote_branches(repo_full, branches)
        if errors:
            raise RuntimeError("删除远端分支失败：" + "；".join(errors))
        return {"branches_deleted": branches}

    def _recollect_side(self, job_id: str, side_name: str) -> None:
        import shutil
        job = self.store.get_job(job_id)
        side = job["sides"][side_name]
        wdir = side.get("workspace", "")
        if not wdir or not Path(wdir).is_dir():
            return
        # Only a genuinely complete turn is eligible evidence: a newest session
        # whose tail is a gateway API Error or a dangling tool_result is a cut
        # turn and must not be re-bound over a side.
        complete = [
            s for s in ws.find_session_jsonl(wdir)
            if ws.transcript_interruption_reason(s["path"]) is None
        ]
        if not complete:
            raise RuntimeError(
                f"{side_name} 侧工作区里找不到完整首轮会话（最新会话疑似被网关截断）；请点「重跑该侧」"
            )
        chosen = complete[0]
        kept = self.store.evidence_dir(job_id) / f"{side_name.lower()}-{chosen['session_id']}.jsonl"
        shutil.copyfile(chosen["path"], kept)
        self.store.update_side(job_id, side_name, {
            "session_id": chosen["session_id"], "jsonl_local": str(kept),
        })

    def side_action(self, body: dict) -> dict:
        job_id, side_name, action = body["job"], body["side"], body["action"]
        if action == "retry":
            self._ensure_review_unlocked(job_id)
            self._ensure_not_archived(job_id)
            self.runner.retry_side(job_id, side_name)
            return {"retry": True}
        if action == "abort":
            self.runner.abort_side(job_id, side_name)
            return {"aborted": True}
        raise RuntimeError(f"未知操作 {action}")

    def review_save(self, body: dict) -> dict:
        """Persist the human GSB judgement.

        Saving with ``lock=True`` + the no-AI attestation flips the pair into
        the review-locked gate (run/retry/prepare refused); ``unlock=True``
        deliberately releases it so evidence can be corrected.
        """
        from .desk_store import utc_now
        job_id = body["job"]
        patch = {k: body.get(k) for k in ("validity", "conclusion", "reason", "reviewer", "note")
                 if k in body}
        for side in ("a", "b"):
            score_key = f"{side}_delivery_score"
            description_key = f"{side}_delivery_description"
            if score_key in body:
                score = str(body[score_key]).strip()
                if score and score not in {"1", "2", "3", "4", "5"}:
                    raise RuntimeError(f"{side.upper()} 交付完整性只能填写 1-5")
                patch[score_key] = score
            if description_key in body:
                patch[description_key] = str(body[description_key]).strip()
        job = self.store.get_job(job_id)
        review = job.get("review", {})
        if body.get("unlock"):
            patch.update({"ai_confirmed": False, "ai_confirmed_at": "", "locked_at": ""})
        elif body.get("lock"):
            if not body.get("ai_confirmed"):
                raise RuntimeError("请先勾选「我确认未使用任何 AI 分析轨迹/产物」再锁定")
            if not str(body.get("reason", "")).strip():
                raise RuntimeError("请先填写 GSB 理由再锁定")
            if job.get("delivery_quality_required") and body.get("validity", review.get("validity")) == "有效":
                for side in ("a", "b"):
                    score = patch.get(f"{side}_delivery_score", review.get(f"{side}_delivery_score", ""))
                    description = patch.get(f"{side}_delivery_description", review.get(f"{side}_delivery_description", ""))
                    if score not in {"1", "2", "3", "4", "5"} or not description:
                        raise RuntimeError(f"请填写 {side.upper()} 交付完整性评分（1-5）和缺陷描述再锁定")
            patch.update({"ai_confirmed": True,
                          "ai_confirmed_at": review.get("ai_confirmed_at") or utc_now(),
                          "locked_at": utc_now()})
        return self.store.update_review(job_id, patch)

    @staticmethod
    def _checklist_fingerprint(job: dict) -> tuple:
        """Evidence fields the completeness report depends on.

        ``updated_at`` is second-resolution, so two writes in the same second
        would look unchanged; fingerprint the actual overlay inputs instead.
        """
        a, b = job["sides"]["A"], job["sides"]["B"]
        r = job.get("review") or {}
        def side_key(s: dict) -> tuple:
            return (s.get("status"), s.get("jsonl_local"), s.get("trace_url"),
                    s.get("video_local"), s.get("video_url"), s.get("head_sha"),
                    s.get("head_url"), bool(s.get("pushed")), s.get("session_id"))
        return (side_key(a), side_key(b),
                r.get("validity"), r.get("conclusion"), r.get("reason"),
                r.get("a_delivery_score"), r.get("a_delivery_description"),
                r.get("b_delivery_score"), r.get("b_delivery_description"),
                r.get("locked_at"), job.get("prompt"), job.get("harness_version"),
                job.get("baseline_sha"), job.get("baseline_url"),
                bool(job.get("baseline_pushed")))

    def public_job(self, job: dict) -> dict:
        """API view: overlay live-process flags so the UI matches the runner."""
        out = json.loads(json.dumps(job))
        for name in ("A", "B"):
            live = self.runner.is_side_live(out["id"], name)
            out["sides"][name]["live"] = live
        fp = self._checklist_fingerprint(job)
        out["check_fp"] = repr(fp)
        cached = self._checklist_cache.get(out["id"])
        if cached and cached.get("_fp") == fp:
            out["check"] = {k: v for k, v in cached.items() if k != "_fp"}
        return out

    def job_view(self, job_id: str, online: bool = False) -> dict:
        job = self.store.get_job(job_id)
        report = run_checklist(job, online=online)
        self._checklist_cache[job_id] = {**report, "_fp": self._checklist_fingerprint(job)}
        out = self.public_job(job)
        out["check"] = report
        return out

    def _job_workspaces(self, job: dict) -> dict[str, str]:
        return {s: (job["sides"][s].get("workspace") or "") for s in ("A", "B")}

    def ports_scan(self, body: dict) -> dict:
        job = self.store.get_job(body["job"])
        # Previous pair's ExclusiveServer lives in this same python.exe
        # (e.g. LP lab still on :8080). taskkill cannot reap our own pid.
        dropped = self.previews.stop_others(job["id"])
        extra_skip: set[int] = set()
        owned_ports: set[int] = set()
        for side in ("A", "B"):
            st = self.previews.status(job["id"], side)
            if st.get("running") and st.get("port"):
                extra_skip.add(int(st["port"]))
                owned_ports.add(int(st["port"]))
        report = ports_mod.gsb_scan(
            workspaces=self._job_workspaces(job), desk_port=self.port,
            extra_skip=extra_skip, owned_ports=owned_ports)
        if dropped:
            report["dropped_previews"] = dropped
        for side in ("A", "B"):
            if side in report.get("sides", {}):
                report["sides"][side]["preview"] = self.previews.status(job["id"], side)
        return report

    def ports_probe(self, body: dict) -> dict:
        url = str(body.get("url") or "").strip()
        ports_mod.assert_loopback_http_url(url)
        return ports_mod.probe_http(url)

    def preview_stop(self, body: dict) -> dict:
        job_id = body.get("job")
        side = str(body.get("side") or "").upper()
        if job_id and side in ("A", "B"):
            return self.previews.stop_side(job_id, side)
        if job_id:
            return {
                "stopped": True,
                "A": self.previews.stop_side(job_id, "A"),
                "B": self.previews.stop_side(job_id, "B"),
            }
        self.previews.stop_all()
        return {"stopped": True}

    def preview_start(self, body: dict) -> dict:
        job = self.store.get_job(body["job"])
        side = str(body.get("side") or "").upper()
        if side not in ("A", "B"):
            raise RuntimeError("side 必须是 A 或 B")
        wdir = job["sides"][side].get("workspace") or ""
        if not wdir or not Path(wdir).is_dir():
            raise RuntimeError(f"{side} 侧工作区不存在")
        occupied = {int(r["port"]) for r in ports_mod.list_listeners() if r.get("port")}
        # Never sit on 8080/5173: those are where Steam and leftover labs linger.
        skip = {int(self.port)} | occupied | set(ports_mod.COMMON_DEV_PORTS)
        return self.previews.start(
            job["id"], side, wdir, preferred=None, skip=skip)

    def ports_overview(self, body: dict | None = None) -> dict:
        """Desk-wide listeners: in-process previews + leftover product servers."""
        import os
        jobs = {j["id"]: j for j in self.store.list_jobs()}
        previews = []
        preview_ports: set[int] = set()
        for item in self.previews.list_all():
            key = str(item.get("key") or "")
            job_id, _, side = key.partition("/")
            job = jobs.get(job_id) or {}
            port = int(item.get("port") or 0)
            if port:
                preview_ports.add(port)
            previews.append({
                **item,
                "job": job_id,
                "side": side,
                "job_name": job.get("name") or job_id,
            })
        leftovers = []
        desk_pid = os.getpid()
        for row in ports_mod.list_listeners():
            port = int(row.get("port") or 0)
            pid = int(row.get("pid") or 0)
            if port == int(self.port) or port in preview_ports:
                continue
            if port not in ports_mod.COMMON_DEV_PORTS:
                continue
            name = row.get("name") or ""
            kind = ports_mod.classify_process_name(name)
            leftovers.append({
                "addr": row.get("addr") or "127.0.0.1",
                "port": port,
                "pid": pid,
                "name": name,
                "class": kind,
                "killable": kind == "product" and pid not in (0, desk_pid),
                "url": f"http://127.0.0.1:{port}/",
            })
        return {
            "desk_port": int(self.port),
            "previews": previews,
            "leftovers": leftovers,
        }

    def ports_reap(self, body: dict | None = None) -> dict:
        import os
        self.previews.stop_all()
        return {"previews_stopped": True,
                **ports_mod.reap_leftover_listeners(keep_pids={os.getpid()})}

    def open_workspace(self, body: dict) -> dict:
        import os
        job = self.store.get_job(body["job"])
        wdir = _side_workspace(job, str(body.get("side") or ""))
        # Only the job's own workspace; never pass an arbitrary client path.
        if sys.platform == "win32":
            os.startfile(str(wdir))  # noqa: S606 — operator-triggered Explorer
        else:
            subprocess.Popen(["xdg-open", str(wdir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"opened": str(wdir)}

    def open_shell(self, body: dict) -> dict:
        if sys.platform != "win32":
            raise RuntimeError("打开 PowerShell 仅支持 Windows")
        job = self.store.get_job(body["job"])
        side = str(body.get("side") or "").upper()
        wdir = _side_workspace(job, side)
        title = f"ATK {side} · {job.get('name') or job.get('id') or ''}".strip(" ·")
        args, kwargs = _powershell_popen(wdir, title=title)
        subprocess.Popen(args, **kwargs)
        return {"opened": str(wdir), "kind": "shell"}

    # ---------- http handler ----------
    def make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            # Hostnames a browser is allowed to reach this loopback server under.
            _LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

            def log_message(self, *_):  # quiet
                pass

            def _reject(self, status: int, message: str) -> bool:
                self._send({"ok": False, "error": message}, status)
                return False

            def _host_ok(self) -> bool:
                """Allow only loopback Host headers (defeats DNS rebinding)."""
                host = (self.headers.get("Host") or "").strip()
                if not host:
                    return self._reject(400, "缺少 Host 头")
                hostname = (urllib.parse.urlsplit("//" + host).hostname or "").lower()
                if hostname not in self._LOOPBACK_HOSTS:
                    return self._reject(403, "拒绝非本地 Host（防 DNS rebinding）")
                return True

            def _origin_ok(self) -> bool:
                """When an Origin is present (browsers always send one on POST),
                require it to be the same loopback origin — a cross-site fetch
                is rejected before any action runs."""
                origin = (self.headers.get("Origin") or "").strip()
                if not origin:
                    return True  # non-browser clients (curl, same-origin GET) send none
                parsed = urllib.parse.urlsplit(origin)
                hostname = (parsed.hostname or "").lower()
                if hostname not in self._LOOPBACK_HOSTS:
                    return self._reject(403, "拒绝跨域来源")
                if parsed.port and parsed.port != self.server.server_address[1]:
                    return self._reject(403, "跨端口来源被拒绝")
                return True

            def _send(self, value, status=200):
                data = json.dumps(value, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception as exc:
                    if _is_client_gone(exc):
                        return
                    raise

            def _body(self):
                # A cross-origin "simple" POST can only send form/plain content
                # types; demanding application/json forces a CORS preflight the
                # loopback server never grants (no Access-Control headers).
                ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if ctype != "application/json":
                    return None  # caller responds 415
                n = int(self.headers.get("Content-Length", "0"))
                if not n:
                    return {}
                try:
                    return json.loads(self.rfile.read(n).decode("utf-8"))
                except json.JSONDecodeError:
                    return {}

            def do_GET(self):
                if not (self._host_ok() and self._origin_ok()):
                    return
                path = urllib.parse.urlparse(self.path).path
                if path == "/":
                    data = PAGE.replace("__TASK_TYPES__", json.dumps(TASK_TYPES, ensure_ascii=False)) \
                              .replace("__DIFF__", json.dumps(DIFFICULTIES, ensure_ascii=False)) \
                              .replace("__CONCLUSIONS__", json.dumps(CONCLUSIONS, ensure_ascii=False)) \
                              .replace("__VALIDITY__", json.dumps(VALIDITY, ensure_ascii=False)) \
                              .replace("__REPRO__", json.dumps(REPRO_LEVELS, ensure_ascii=False))
                    raw = data.encode("utf-8")
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                    except Exception as exc:
                        if not _is_client_gone(exc):
                            raise
                    return
                if path == "/settings":
                    raw = SETTINGS_PAGE.encode("utf-8")
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                    except Exception as exc:
                        if not _is_client_gone(exc):
                            raise
                    return
                self._send({"error": "not found"}, 404)

            def do_POST(self):
                if not (self._host_ok() and self._origin_ok()):
                    return
                path = urllib.parse.urlparse(self.path).path
                body = self._body()
                if body is None:
                    self._send({"ok": False, "error": "仅接受 Content-Type: application/json"}, 415)
                    return
                try:
                    self._send(self._route(path, body))
                except FileNotFoundError as exc:
                    self._send({"ok": False, "error": str(exc)}, 404)
                except Exception as exc:
                    if _is_client_gone(exc):
                        return
                    self._send({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400)

            def _route(self, path: str, body: dict):
                if path == "/api/settings_get":
                    s = server.store.settings()
                    return {**s, "secret_path": str(server.store.secrets_path),
                            "oss": oss_mod.describe_config(server.oss_cfg())}
                if path == "/api/settings_save":
                    saved = dict(server.store.save_settings(body))
                    saved["workers"] = server.runner.ensure_workers()
                    return saved
                if path == "/api/oss_test":
                    cfg = server.oss_cfg(body)
                    return oss_mod.connection_test(cfg) if cfg else {"ok": False, "error": "OSS 配置不完整"}
                if path == "/api/oss_create":
                    cfg = server.oss_cfg(body)
                    if cfg is None:
                        return {"ok": False, "error": "OSS 配置不完整"}
                    return oss_mod.ensure_bucket(cfg)
                if path == "/api/jobs":
                    return {"jobs": [server.public_job(j) for j in server.store.list_jobs()]}
                if path == "/api/repo_check":
                    return server.repo_check(body)
                if path == "/api/gh_status":
                    return server.gh_status()
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
                    return server.review_save(body)
                if path == "/api/export":
                    job = server.store.get_job(body["job"])
                    out = server.store.evidence_dir(body["job"]) / "submission.tsv"
                    return export_tsv(job, out, strict=True)
                if path == "/api/log":
                    p = server.store.evidence_dir(body["job"]) / f"{body['side'].lower()}-run.log"
                    return {"log": p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""}
                if path == "/api/log_tail":
                    p = server.store.evidence_dir(body["job"]) / f"{body['side'].lower()}-run.log"
                    try:
                        offset = int(body.get("offset", 0))
                    except (TypeError, ValueError):
                        offset = 0
                    if not p.exists():
                        return {"text": "", "offset": 0, "size": 0, "mtime": 0}
                    size = p.stat().st_size
                    # File was rotated/truncated (new attempt rewrites the log): restart.
                    if offset > size:
                        offset = 0
                    with p.open("rb") as f:
                        f.seek(offset)
                        raw = f.read(96 * 1024)
                    return {"text": raw.decode("utf-8", errors="replace"),
                            "offset": min(size, offset + len(raw)),
                            "size": size, "mtime": p.stat().st_mtime}
                if path == "/api/pick_file":
                    return server._pick_file(body.get("side", "A"))
                if path == "/api/rec_windows":
                    return {"windows": server.recorder.list_windows()}
                if path == "/api/rec_start":
                    return server.recorder.start(
                        body["job"], body["side"],
                        window=body.get("window", ""),
                        max_seconds=int(body.get("max_seconds") or 0),
                        fps=int(body.get("fps") or 0),
                    )
                if path == "/api/rec_stop":
                    return server.recorder.stop(body["job"], body["side"])
                if path == "/api/rec_status":
                    return server.recorder.status(body["job"], body["side"])
                if path == "/api/ports_scan":
                    return server.ports_scan(body)
                if path == "/api/ports_probe":
                    return server.ports_probe(body)
                if path == "/api/preview_start":
                    return server.preview_start(body)
                if path == "/api/preview_stop":
                    return server.preview_stop(body)
                if path == "/api/ports_overview":
                    return server.ports_overview(body)
                if path == "/api/ports_reap":
                    return server.ports_reap(body)
                if path == "/api/open_workspace":
                    return server.open_workspace(body)
                if path == "/api/open_shell":
                    return server.open_shell(body)
                return {"ok": False, "error": f"unknown path {path}"}

        return Handler

    def gh_status(self) -> dict:
        """Report the authenticated gh login (settings page indicator)."""
        try:
            return {"login": self._gh_login()}
        except ghutil.GitHubError as exc:
            return {"login": "", "error": str(exc)}

    def repo_check(self, body: dict) -> dict:
        """Validate a repo name and probe the remote (existence/visibility).

        Never raises for an expected gh failure: the UI keeps working and shows
        the gh error (e.g. not logged in) next to the still-usable local path.
        """
        name = str(body.get("name", "")).strip()
        problem = ghutil.validate_repo_name(name)
        if problem:
            return {"name": name, "syntax_error": problem, "exists": False}
        settings = self.store.settings()
        configured_owner = str(settings.get("github_owner", "")).strip()
        parent = clean_path(body.get("parent_dir", "") or settings.get("baseline_parent_dir", ""))
        local_path = str(Path(parent) / name) if parent else name
        try:
            cli = ghutil.CliGh()
            login = cli.viewer_login()
            if configured_owner and configured_owner.lower() != login.lower():
                return {"name": name, "full_name": "", "exists": False,
                        "gh_error": f"后台设置的归属账号 {configured_owner} 与当前 gh 登录账号 {login} 不一致",
                        "local_path": local_path}
            repo = cli.repo_view(login, name)
        except ghutil.GitHubError as exc:
            return {"name": name, "full_name": "", "exists": False,
                    "gh_error": str(exc), "local_path": local_path}
        if repo is None:
            return {"name": name, "full_name": f"{login}/{name}", "exists": False,
                    "local_path": local_path}
        return {"name": repo.name, "full_name": repo.full_name, "exists": True,
                "visibility": repo.visibility.lower(), "default_branch": repo.default_branch,
                "url": repo.url, "local_path": local_path}

    def _pick_file(self, side: str) -> dict:
        """Native file dialog via PowerShell (runs on the desktop, not a service)."""
        import subprocess
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$f=New-Object System.Windows.Forms.OpenFileDialog;"
            "$f.Filter='Video (*.mp4)|*.mp4|All (*.*)|*.*';"
            "if($f.ShowDialog() -eq 'OK'){[Console]::OutputEncoding=[Text.Encoding]::UTF8;$f.FileName}"
        )
        # Hidden console only — do not CREATE_NO_WINDOW, or OpenFileDialog
        # may never appear (it needs a desktop window).
        p = subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-STA", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        return {"path": p.stdout.strip()}

    def serve(self, open_browser: bool = True) -> int:
        # 1) single-instance lock, 2) bind port — both BEFORE any recovery/run,
        # so a duplicate launch cannot mutate state or spawn extra claude runs.
        if not self._acquire_singleton_lock():
            print(f"已有一个 Pair 交付台在运行（占用数据目录 {self.store.home}），本次启动退出。")
            return 2
        try:
            httpd = _ExclusiveServer(("127.0.0.1", self.port), self.make_handler())
        except OSError as exc:
            print(f"端口 {self.port} 已被占用，交付台可能已在运行：{exc}")
            return 2
        # Accept HTTP before recover() recopies workspaces, otherwise the
        # supervisor health check treats a listening-but-blocked worker as dead.
        http_thread = threading.Thread(target=httpd.serve_forever, name="desk-http", daemon=True)
        http_thread.start()
        self.runner.start()
        url = f"http://127.0.0.1:{self.port}/"
        if open_browser:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        print(f"Pair 交付台运行中: {url}\n数据目录: {self.store.home}\nCtrl+C 退出（任务状态已落盘，重开自动恢复）")
        try:
            while http_thread.is_alive():
                http_thread.join(timeout=0.5)
        except KeyboardInterrupt:
            httpd.shutdown()
        finally:
            self.runner.stop()
            self.recorder.stop_all()
            self.previews.stop_all()
            httpd.server_close()
            if self._lock_fp:
                try:
                    self._lock_fp.close()
                except OSError:
                    pass
        return 0


def main(argv=None) -> int:
    import argparse
    from .desk_service import DEFAULT_PORT, desk_url, is_running, start_desk, stop_desk, supervise

    ap = argparse.ArgumentParser(
        prog="atk desk",
        description="Pair 交付台。默认在后台常驻（关终端也不停），用「退出交付台」或 desk stop 停止。",
    )
    ap.add_argument(
        "action", nargs="?", default="start",
        choices=["start", "stop", "status", "foreground"],
        help="start=后台应用（默认） stop=停止 status=查看 foreground=挂在当前终端",
    )
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--app", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--foreground", action="store_true")
    args = ap.parse_args(argv)

    if args.worker:
        return DeskServer(DeskStore(), port=args.port).serve(open_browser=False)
    if args.supervise:
        return supervise(args.port)
    if args.app:
        from .desk_app import run_app
        return run_app(args.port)
    if args.action == "stop":
        out = stop_desk(args.port)
        print("已停止 Pair 交付台" if out.get("stopped") else "交付台未在运行（或正在退出）")
        return 0
    if args.action == "status":
        info = is_running(args.port)
        if info["http"]:
            print(f"运行中  {info['url']}  worker={info['worker_pid'] or '-'}  supervisor={info['supervisor_pid'] or '-'}")
            return 0
        print("未在运行。启动：python -m agent_trace_kit.desk")
        return 1
    if args.foreground or args.action == "foreground":
        return DeskServer(DeskStore(), port=args.port).serve(open_browser=not args.no_browser)

    info = start_desk(args.port, open_browser=not args.no_browser, open_app=True)
    url = info.get("url") or desk_url(args.port)
    if info.get("already") or info.get("http"):
        print(f"Pair 交付台已在后台运行: {url}")
        print("任务栏有「Pair 交付台」窗口。关掉那个窗口，或执行 python -m agent_trace_kit.desk stop")
        return 0
    print(f"Pair 交付台已在后台启动: {url}")
    print("关 PowerShell / 浏览器都不会停。要停：关掉任务栏窗口，或 python -m agent_trace_kit.desk stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
