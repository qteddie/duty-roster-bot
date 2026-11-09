from __future__ import annotations

import json
import threading
import webbrowser
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image

from .context import core_box, load_context
from .schema import ALLOWED_CODES, Annotation, Box, CellAnnotation, load_annotations, save_annotations


def _clamped_box(payload: dict, width: int, height: int) -> Box:
    x1 = max(0, min(width - 1, int(payload["x1"])))
    y1 = max(0, min(height - 1, int(payload["y1"])))
    x2 = max(x1 + 1, min(width, int(payload["x2"])))
    y2 = max(y1 + 1, min(height, int(payload["y2"])))
    return Box(x1, y1, x2, y2)

PAGE = r'''<!doctype html><html lang="zh-Hant"><meta charset="utf-8">
<title>全員代號框選</title><style>
body{font:15px system-ui;margin:0;background:#f3f4f6;color:#111827}header{position:sticky;top:0;background:white;padding:10px;display:flex;gap:8px;align-items:center;z-index:2;box-shadow:0 1px 5px #bbb;flex-wrap:wrap}button,input,select{font:inherit;padding:7px}main{display:grid;grid-template-columns:minmax(420px,1fr) 360px;gap:16px;padding:16px}canvas{background:white;width:min(58vw,560px);height:auto;image-rendering:auto;cursor:crosshair;border:1px solid #9ca3af}.item{padding:8px;border-bottom:1px solid #ddd;display:grid;grid-template-columns:1fr 100px 32px;gap:5px}.hint{color:#4b5563}.primary{background:#2563eb;color:white;border:0;border-radius:4px}.proposal{color:#1d4ed8}#status{margin-left:auto}</style>
<header><select id="filter"><option value="review">待審核</option><option value="uncertain">低信心</option><option value="reviewed">已複核</option><option value="all">全部</option></select><button id="reload">套用</button><button id="prev">←</button><button id="next">→</button><b id="title"></b><button id="empty">E 空白</button><button id="save" class="primary">A 接受／儲存</button><button id="bulk">接受全部 ≥95%</button><span id="status"></span></header>
<main><section><canvas id="canvas"></canvas><p class="hint">先從右側選擇員工，再拖曳框住完整人員代號。同格多人就建立多個框。工作編號請選 other，看不清楚選 unknown，殘缺選 partial。Delete 可刪除選取框。</p></section><aside><input id="search" placeholder="搜尋代號或姓名" style="width:95%"><b id="selected"></b><div id="people" style="max-height:280px;overflow:auto;margin:8px 0"></div><hr><div id="list"></div></aside></main>
<script>
let rows=[],codes=[],i=0,boxes=[],active=-1,start=null,img=new Image(),activeCode='1';const c=document.querySelector('#canvas'),x=c.getContext('2d');
function renderPeople(){let query=document.querySelector('#search').value.trim().toLowerCase(),e=document.querySelector('#people');e.innerHTML='';codes.filter(p=>!query||p.code.toLowerCase().includes(query)||(p.name||'').toLowerCase().includes(query)).forEach(p=>{let b=document.createElement('button');b.textContent=p.name?`${p.code} ${p.name}`:p.code;b.style.margin='2px';b.style.background=p.code===activeCode?'#bfdbfe':'';b.onclick=()=>{activeCode=p.code;document.querySelector('#selected').textContent=`目前：${p.code} ${p.name||''}`;renderPeople()};e.append(b)})}
function draw(){x.clearRect(0,0,c.width,c.height);x.drawImage(img,0,0);boxes.forEach((b,n)=>{x.strokeStyle=n===active?'#ef4444':b.role==='personnel_code'?'#16a34a':'#f59e0b';x.lineWidth=2;x.strokeRect(b.x1,b.y1,b.x2-b.x1,b.y2-b.y1)});renderList()}
function renderList(){let e=document.querySelector('#list');e.innerHTML='';boxes.forEach((b,n)=>{let d=document.createElement('div');d.className='item';if(b.source&&b.source!=='human')d.classList.add('proposal');let t=document.createElement('input');t.value=b.text;t.placeholder='代號';t.oninput=()=>{b.text=t.value.trim().toUpperCase();b.source='human';b.confidence=null};let s=document.createElement('select');['personnel_code','other','unknown','partial'].forEach(v=>{let o=document.createElement('option');o.value=o.textContent=v;o.selected=b.role===v;s.append(o)});s.onchange=()=>{b.role=s.value;b.source='human';b.confidence=null};let q=document.createElement('button');q.textContent='×';q.onclick=()=>{boxes.splice(n,1);active=-1;draw()};d.onclick=()=>{active=n;draw()};d.title=b.confidence==null?'人工':`自動建議，信心 ${Math.round(b.confidence*100)}%`;d.append(t,s,q);e.append(d)})}
function load(){if(!rows.length){document.querySelector('#title').textContent='此佇列已完成';x.clearRect(0,0,c.width,c.height);return}let r=rows[i];boxes=(r.annotations||[]).map(a=>({...a.box,text:a.text,role:a.role,source:a.source,confidence:a.confidence}));active=-1;document.querySelector('#title').textContent=`${i+1}/${rows.length} r${r.row} c${r.column}`;img.onload=()=>{c.width=img.naturalWidth;c.height=img.naturalHeight;draw()};img.src='/image/'+encodeURIComponent(r.image)+'?v='+Date.now();let confidence=boxes.length?Math.min(...boxes.map(b=>b.confidence??1)):null;document.querySelector('#status').textContent=`${r.reviewed?'已複核':'待審'}${confidence==null?'':`｜最低 ${Math.round(confidence*100)}%`}`}
function pos(e){let r=c.getBoundingClientRect();return [Math.round((e.clientX-r.left)*c.width/r.width),Math.round((e.clientY-r.top)*c.height/r.height)]}
c.onpointerdown=e=>{start=pos(e);c.setPointerCapture(e.pointerId)};c.onpointerup=e=>{if(!start)return;let p=pos(e),b={x1:Math.min(start[0],p[0]),y1:Math.min(start[1],p[1]),x2:Math.max(start[0],p[0]),y2:Math.max(start[1],p[1]),text:activeCode,role:'personnel_code'};start=null;if(b.x2-b.x1>3&&b.y2-b.y1>3){boxes.push(b);active=boxes.length-1;draw()}};
async function save(next=true){let r=rows[i];let response=await fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sample_id:r.sample_id,annotations:boxes,reviewed:true})});let data=await response.json();if(!response.ok){alert(data.error);return}if(document.querySelector('#filter').value==='review'||document.querySelector('#filter').value==='uncertain'){rows.splice(i,1);i=Math.min(i,Math.max(0,rows.length-1))}else{r.reviewed=true}if(next)load();else draw()}
async function reload(){let filter=document.querySelector('#filter').value;rows=await fetch('/api/rows?filter='+filter).then(r=>r.json());i=0;load()}
document.querySelector('#save').onclick=()=>save();document.querySelector('#empty').onclick=()=>{boxes=[];save()};document.querySelector('#prev').onclick=()=>{i=Math.max(0,i-1);load()};document.querySelector('#next').onclick=()=>{i=Math.min(rows.length-1,i+1);load()};document.querySelector('#reload').onclick=reload;document.querySelector('#bulk').onclick=async()=>{if(!confirm('接受所有信心至少 95% 且代號有效的自動建議？'))return;let r=await fetch('/api/accept-confident',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({minimum_confidence:.95})}).then(r=>r.json());alert(`已接受 ${r.accepted} 格`);reload()};
document.onkeydown=e=>{if(e.target.matches('input,select'))return;if(e.key==='Delete'&&active>=0){boxes.splice(active,1);active=-1;draw()}else if(e.key==='ArrowRight'||e.key.toLowerCase()==='s'){i=Math.min(rows.length-1,i+1);load()}else if(e.key==='ArrowLeft'){i=Math.max(0,i-1);load()}else if(e.key==='Enter'||e.key.toLowerCase()==='a')save();else if(e.key.toLowerCase()==='e'){boxes=[];save()}};
document.querySelector('#search').oninput=renderPeople;Promise.all([fetch('/api/codes').then(r=>r.json()),reload()]).then(v=>{codes=v[0];renderPeople()});
</script></html>'''


def merge_model_proposals(items: list[CellAnnotation], proposals: list[dict]) -> tuple[list[CellAnnotation], int]:
    indexes = {item.sample_id: n for n, item in enumerate(items)}
    updated = list(items); applied = 0
    for proposal in proposals:
        index = indexes.get(proposal["sample_id"])
        if index is None:
            continue
        current = updated[index]
        if current.reviewed or any(ann.source == "human" for ann in current.annotations):
            continue
        if current != CellAnnotation.from_dict(proposal["expected"]):
            continue
        annotations = tuple(Annotation(Box(**ann["box"]), ann["text"], ann["role"],
                                       "model", ann["confidence"]) for ann in proposal["annotations"])
        candidate = replace(current, annotations=annotations, reviewed=False)
        candidate.validate(); updated[index] = candidate; applied += 1
    return updated, applied


def serve(dataset: Path, host: str, port: int, *, roster: Path | None = None,
          open_browser=True) -> None:
    manifest = dataset / "annotations.jsonl"
    context = load_context(dataset)
    lock = threading.Lock()
    names = json.loads(roster.read_text(encoding="utf-8")) if roster and roster.is_file() else {}

    class Handler(BaseHTTPRequestHandler):
        def send_payload(self, payload: bytes, content_type="application/json", status=200):
            self.send_response(status); self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload))); self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(payload)

        def do_GET(self):
            parsed = urlparse(self.path); route = parsed.path
            if route == "/":
                page = Path(__file__).with_name("annotation_page.html").read_bytes()
                return self.send_payload(page, "text/html; charset=utf-8")
            if route == "/api/rows":
                selected = parse_qs(parsed.query).get("filter", ["review"])[0]
                items = load_annotations(manifest)
                if selected == "review": items = [item for item in items if not item.reviewed]
                elif selected == "uncertain": items = [item for item in items if not item.reviewed and any(
                    ann.role == "unknown" or (ann.confidence is not None and ann.confidence < .8)
                    for ann in item.annotations)]
                elif selected == "reviewed": items = [item for item in items if item.reviewed]
                # Interleave dates for early seed labels so train/val/test all
                # receive independent source photos before the first model run.
                items.sort(key=lambda item: (item.row, item.column, item.source_group))
                payload = [item.to_dict() for item in items]
                if context.get("fx") or context.get("fy"):
                    for row in payload:  # dashed outline of the cell that owns the labels
                        row["core"] = core_box(row["width"], row["height"], context)
                return self.send_payload(json.dumps(payload, ensure_ascii=False).encode())
            if route == "/api/codes":
                payload = [{"code": code, "name": names.get(code, "")} for code in ALLOWED_CODES]
                return self.send_payload(json.dumps(payload, ensure_ascii=False).encode())
            if route.startswith("/image/"):
                relative = unquote(route[len("/image/"):])
                path = (dataset / relative).resolve()
                try: path.relative_to(dataset.resolve())
                except ValueError: return self.send_error(HTTPStatus.FORBIDDEN)
                if not path.is_file(): return self.send_error(HTTPStatus.NOT_FOUND)
                return self.send_payload(path.read_bytes(), "image/png")
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self):
            route = urlparse(self.path).path
            if route == "/api/model-proposals":
                try:
                    payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                    with lock:
                        items, applied = merge_model_proposals(load_annotations(manifest), payload["proposals"])
                        save_annotations(manifest, items)
                    return self.send_payload(json.dumps({"applied": applied,
                                                        "skipped": len(payload["proposals"])-applied}).encode())
                except Exception as exc:
                    return self.send_payload(json.dumps({"error": str(exc)}).encode(), status=400)
            if route == "/api/accept-confident":
                try:
                    payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                    minimum = float(payload.get("minimum_confidence", .95)); accepted = 0
                    with lock:
                        items = load_annotations(manifest)
                        for index, item in enumerate(items):
                            if (not item.reviewed and item.annotations and all(
                                ann.role == "personnel_code" and ann.text in ALLOWED_CODES and
                                ann.confidence is not None and ann.confidence >= minimum
                                for ann in item.annotations)):
                                items[index] = replace(item, reviewed=True); accepted += 1
                        save_annotations(manifest, items)
                    return self.send_payload(json.dumps({"accepted": accepted}).encode())
                except Exception as exc:
                    return self.send_payload(json.dumps({"error": str(exc)}).encode(), status=400)
            if route != "/api/save": return self.send_error(HTTPStatus.NOT_FOUND)
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                with lock:
                    items = load_annotations(manifest)
                    index = next(i for i, item in enumerate(items) if item.sample_id == payload["sample_id"])
                    current = items[index]
                    with Image.open(dataset / current.image) as image:
                        width, height = image.size
                    annotations = tuple(Annotation(_clamped_box(v, width, height),
                                                   str(v.get("text", "")).strip().upper(),
                                                   str(v.get("role", "personnel_code")),
                                                   "human", None)
                                        for v in payload.get("annotations", []))
                    updated = replace(current, width=width, height=height,
                                      reviewed=bool(payload.get("reviewed")), annotations=annotations)
                    updated.validate(); items[index] = updated; save_annotations(manifest, items)
                self.send_payload(b'{"ok":true}')
            except Exception as exc:
                self.send_payload(json.dumps({"error": str(exc)}, ensure_ascii=False).encode(), status=400)

        def log_message(self, *_): pass

    url = f"http://{host}:{port}"
    if open_browser: webbrowser.open(url)
    print(f"全員代號標註介面：{url}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
