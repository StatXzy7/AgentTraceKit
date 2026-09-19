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

from . import ghutil
from . import oss as oss_mod
from . import workspace as ws
from .checklist import run_checklist
from .desk_store import CONCLUSIONS, DIFFICULTIES, REPRO_LEVELS, TASK_TYPES, VALIDITY, DeskStore, clean_path
from .export_tsv import HEADERS, export_tsv, job_row, upload_side
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
  <h2 style="margin:0 0 8px">任务队列</h2>
  <table><thead><tr><th>状态</th><th>任务</th><th>类型/难度</th><th>A</th><th>B</th><th>证据</th><th>GSB</th><th></th></tr></thead>
  <tbody id="jobRows"><tr><td colspan="8" class="muted">加载中…</td></tr></tbody></table>
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
    <div class="kv" style="margin:6px 0">${j.github_url?`仓库：<a href="${esc(j.github_url)}" target="_blank" class="mono">${esc(j.github_repo||j.github_url)}</a>${j.github_created===true?"（本次新建）":""} · `:""}基线：${j.baseline_url?`<a href="${esc(j.baseline_url)}" target="_blank" class="mono">${esc(j.baseline_sha.slice(0,12))}</a>`:"未准备"} · ${esc(j.harness)} ${esc(j.harness_version)} · ${esc(j.os_name)}</div>
    <div class="grid">
      ${sideHtml(j,"A")}${sideHtml(j,"B")}
    </div>
    <div class="btns">
      <button onclick="act('prepare')">① 重新准备/校验基线</button>
      <button class="sec" onclick="act('enqueue')">② 开始/重试运行</button>
      <button class="ghost" onclick="refreshDetail()">↻ 刷新检查</button>
      <button class="ghost" onclick="act('collect')">重新采集会话</button>
      <button class="ghost" onclick="followOnly=null;startFollow()">📡 同时跟随 A/B</button>
      <button class="ghost" style="margin-left:auto;color:#b91c1c" onclick="deleteJob()">删除任务（清理工作区和证据）</button>
    </div>
    <div class="grid" style="margin-top:8px">
      <div class="sidebox"><h3>🎥 A 侧录屏</h3>
        <div id="recA" class="kv muted">未开始</div>
        <div class="btns" style="margin-top:6px">
          <button onclick="recStart('A')">● 开始录屏</button>
          <button class="sec" id="recStopA" onclick="recStop('A')" disabled>■ 停止并保存</button>
          <button class="ghost" type="button" onclick="pickVideo('A')">选择已有文件…</button>
        </div>
        <input id="vA" value="${esc(j.sides.A.video_local||"")}" style="margin-top:6px" placeholder="也可直接粘贴 mp4 路径">
      </div>
      <div class="sidebox"><h3>🎥 B 侧录屏</h3>
        <div id="recB" class="kv muted">未开始</div>
        <div class="btns" style="margin-top:6px">
          <button onclick="recStart('B')">● 开始录屏</button>
          <button class="sec" id="recStopB" onclick="recStop('B')" disabled>■ 停止并保存</button>
          <button class="ghost" type="button" onclick="pickVideo('B')">选择已有文件…</button>
        </div>
        <input id="vB" value="${esc(j.sides.B.video_local||"")}" style="margin-top:6px" placeholder="也可直接粘贴 mp4 路径">
      </div>
    </div>
    <p class="kv muted" style="margin:6px 2px">录主屏幕（含声音以外的全部画面），<b>产物运行结束就点停止</b>，没有时长上限；失败也要录。录完点「保存录屏路径」再上传 OSS。</p>
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
    <div class="grid">
      <label>有效性<select id="r_validity"><option value="">请选择</option>${__VALIDITY__.map(x=>`<option ${x===j.review.validity?"selected":""}>${x}</option>`).join("")}</select></label>
      <label>结论<select id="r_conclusion"><option value="">请选择</option>${__CONCLUSIONS__.map(x=>`<option ${x===j.review.conclusion?"selected":""}>${x}</option>`).join("")}</select></label>
    </div>
    <label>GSB 理由（A、B 分别说明；Same 至少 80 字，其余 30 字以上；作废时可简述原因）<textarea id="r_reason" style="min-height:140px">${esc(j.review.reason||"")}</textarea></label>
    <div class="btns"><button onclick="saveReview()">保存 GSB</button>
    <button class="sec" onclick="exportRow()" ${c.ready?"":"disabled"}>⑤ 生成 TSV（全部绿灯后可用）</button></div>
    <div id="tsvBox" style="display:none;margin-top:10px">
      <pre class="log" id="tsvPre"></pre>
      <div class="btns"><button onclick="copyTsv()">复制 TSV（粘贴到飞书表格）</button>
      <button class="ghost" onclick="downloadTsv()">下载 .tsv 文件</button></div>
    </div>
  </div>`;
  recRefresh("A");recRefresh("B");
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
  <div class="btns"><button class="ghost" ${run?"disabled":""} title="${run?"运行中不能重跑，请先中止":""}" onclick="sideAct('retry','${s}')">重跑该侧</button>
  <button class="ghost" ${run?"":"disabled"} onclick="sideAct('abort','${s}')">中止</button>
  <button class="ghost" onclick="openLog('${s}')">运行日志</button>
  <button class="ghost" onclick="followSide('${s}')">📡 实时跟随</button></div></div>`}
function checksHtml(items){if(!items||!items.length)return '<span class="muted">点「刷新检查」</span>';
  const groups={};items.forEach(i=>{(groups[i.group]=groups[i.group]||[]).push(i)});
  return Object.entries(groups).map(([g,xs])=>`<div style="margin:6px 0"><b>${esc(g)}</b><br>${xs.map(x=>
   `<span title="${esc(x.detail)}"><span class="dot ${x.ok?"g":x.blocking?"r":"y"}"></span><span class="${x.ok?"ok":x.blocking?"bad":"warn"}">${esc(x.label)}</span></span>`).join("　")}</div>`).join("")}
function closeDetail(){$("detail").style.display="none";SEL=null}
async function act(a){const body={job:SEL,action:a,video_a:$("vA")? $("vA").value:"",video_b:$("vB")?$("vB").value:""};
  await api("/api/job_action",body);if(a==="delete"){closeDetail();await loadJobs();return}await refreshDetail()}
async function deleteJob(){if(!confirm("确定删除该任务？将清理 A/B 工作区、证据文件和任务记录（已 push 的远端分支保留），不可恢复。"))return;await act("delete");toast("任务已删除")}
async function sideAct(a,s){await api("/api/side_action",{job:SEL,side:s,action:a});await refreshDetail()}
async function refreshDetail(online){const j=await api("/api/job",{id:SEL,online:!!online});const f=JOBS.findIndex(x=>x.id===SEL);if(f>=0)JOBS[f]=j;renderRows();renderDetail(SEL)}
async function saveReview(){await api("/api/review",{job:SEL,validity:$("r_validity").value,conclusion:$("r_conclusion").value,reason:$("r_reason").value});toast("GSB 已保存");refreshDetail()}
async function exportRow(){const x=await api("/api/export",{job:SEL});$("tsvBox").style.display="block";$("tsvPre").textContent=x.tsv;window.__tsv=x.tsv}
function copyTsv(){navigator.clipboard.writeText(window.__tsv||"");toast("已复制，去飞书表格粘贴（整行）")}
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
    if(j&&j.sides&&j.sides[s])$("followState"+s).textContent="· "+sideStatus(j.sides[s].status);
    let x;try{x=await fetch("/api/log_tail",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({job:SEL,side:s,offset:followOff[s]})}).then(r=>r.json())}catch(e){continue}
    if(typeof x.offset!=="number")continue;
    if(x.offset<followOff[s]){$("followLog"+s).textContent="";followOff[s]=0} // rotated
    if(x.text){const el=$("followLog"+s);const stick=$("followAutoscroll").checked&&(el.scrollTop+el.clientHeight>=el.scrollHeight-30);el.textContent+=x.text;if(stick)el.scrollTop=el.scrollHeight}
    followOff[s]=x.offset;
  }
}
function clearFollow(){["A","B"].forEach(s=>{followOff[s]=0;$("followLog"+s).textContent=""})}
function closeFollow(){clearInterval(followTimer);followTimer=null;followOnly=null;$("follow").style.display="none";["A","B"].forEach(s=>{const c=$("followLog"+s).closest("div");c.style.display="";c.style.gridColumn=""})}
async function pickVideo(s){const x=await api("/api/pick_file",{side:s});if(x.path){$("v"+s).value=x.path}}

let recTimers={};
async function recStart(s){await api("/api/rec_start",{job:SEL,side:s});toast(s+" 侧开始录屏——切到产物窗口，从干净状态展示真实运行");recRefresh(s)}
async function recStop(s){await api("/api/rec_stop",{job:SEL,side:s});recRefresh(s);await refreshDetail();toast(s+" 侧录屏已保存")}
async function recRefresh(s){
  clearInterval(recTimers[s]);
  let x;try{const r=await fetch("/api/rec_status",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({job:SEL,side:s})});x=await r.json()}catch(e){return}
  const el=$("rec"+s);if(!el)return;
  if(x.recording){
    const btn=$("recStop"+s);if(btn)btn.disabled=false;
    el.innerHTML=`🔴 录制中 <b>${x.elapsed}s</b>（运行结束点「停止并保存」）`;
    el.className="kv bad";
    recTimers[s]=setInterval(()=>recRefresh(s),1000);
  }else{
    const btn=$("recStop"+s);if(btn)btn.disabled=true;
    if(x.path){el.innerHTML=`✓ 已保存 ${x.duration?x.duration.toFixed(0)+"s":""} ${x.size?(x.size/1024/1024).toFixed(1)+"MB":""}<br><span class="muted">${esc(x.path)}</span>`;el.className="kv ok"}
    else{el.textContent="未开始";el.className="kv muted"}
  }
}

loadDefaults().then(loadJobs);
pollTimer=setInterval(()=>{if(JOBS.some(j=>["running","ready"].includes(j.status)||Object.values(j.sides).some(x=>["running","preparing"].includes(x.status))))loadJobs()},5000);
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
  <p class="muted" style="margin:0 0 6px">上游（seed-code 网关）深度思考编码轮经常静默断流或中途 504；遇到断流会自动放弃残缺轮、重新复制干净基线副本并重试，直到拿到完整轮次。</p>
  <div class="grid3">
    <label>claude 命令<input id="s_claude"></label>
    <label>最多并行 pair 数<input id="s_parallel" type="number" min="1" max="6"></label>
    <label>单侧总超时（秒）<input id="s_timeout" type="number" min="300"></label>
    <label>断流判定（秒无活动）<input id="s_stall" type="number" min="30"></label>
    <label>单侧最多自动重试次数（最小 12）<input id="s_attempts" type="number" min="12" max="50"></label>
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
  $("s_timeout").value=SETTINGS.side_timeout_seconds;$("s_stall").value=SETTINGS.stall_seconds;
  $("s_attempts").value=Math.max(12,SETTINGS.side_max_attempts);$("s_poll").value=SETTINGS.activity_poll_seconds;
  $("s_perm").value=SETTINGS.permission_mode||"bypassPermissions";
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
 side_timeout_seconds:+$("s_timeout").value,stall_seconds:+$("s_stall").value,
 side_max_attempts:Math.max(12,+$("s_attempts").value),activity_poll_seconds:+$("s_poll").value,
 permission_mode:$("s_perm").value,
 oss_endpoint:$("s_endpoint").value,oss_region:$("s_region").value,
 oss_bucket:$("s_bucket").value,oss_public_base:$("s_pubbase").value,oss_key_prefix:$("s_prefix").value,
 default_baseline_repo:$("s_baseline").value,
 baseline_parent_dir:$("s_parent").value,github_owner:$("s_owner").value,
 github_private:$("s_private").value==="1",
 reviewer:$("s_reviewer").value};
 SETTINGS=await api("/api/settings_save",body);toast("设置已保存（新任务/下一次重试运行时生效）")}
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


class _ExclusiveServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets two processes bind the same port; refuse it so
    # a second desk fails loudly instead of silently double-running every job.
    allow_reuse_address = False
    daemon_threads = True


class DeskServer:
    def __init__(self, store: DeskStore, port: int = 8765):
        self.store = store
        self.runner = PairRunner(store)
        self.recorder = Recorder(store)
        self.port = port
        self._lock_fp = None
        self._gh_login_cache: tuple[str, float] | None = None

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
        if action == "delete":
            for side_name in ("A", "B"):
                try:
                    self.runner.abort_side(job_id, side_name)
                except Exception:
                    pass
            job = self.store.get_job(job_id)
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
            return {"deleted": True}
        raise RuntimeError(f"未知操作 {action}")

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
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

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
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if path == "/settings":
                    raw = SETTINGS_PAGE.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
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
                    return server.store.update_review(body["job"], body)
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
                if path == "/api/rec_start":
                    return server.recorder.start(body["job"], body["side"])
                if path == "/api/rec_stop":
                    return server.recorder.stop(body["job"], body["side"])
                if path == "/api/rec_status":
                    return server.recorder.status(body["job"], body["side"])
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
        p = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
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
        self.runner.start()
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
            self.recorder.stop_all()
            httpd.server_close()
            if self._lock_fp:
                try:
                    self._lock_fp.close()
                except OSError:
                    pass
        return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="atk desk")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    return DeskServer(DeskStore(), port=args.port).serve(open_browser=not args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
