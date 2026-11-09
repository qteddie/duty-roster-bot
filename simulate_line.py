#!/usr/bin/env python3
"""Run the real local-ResNet LINE analysis and retain a request/reply record.

No messages are sent to LINE or OpenAI. Every invocation gets a new run directory.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')

from backend.schedule_service import (
    ScheduleUploadError,
    _line_phase1_template,
    analyze_schedule_image,
    create_corrected_schedule_preview,
)
from backend.api_usage import summarize_usage, usage_record
from backend.phase1_multilabel_compare import MULTILABEL_INSTRUCTIONS, PRODUCTION_INNER_FRACTION
from backend.batch_reader import MARKDOWN_INSTRUCTIONS, MARKDOWN_REVIEW_INSTRUCTIONS
from line_bot import format_line_report, format_line_total_price, format_line_error

ACK = '已收到班表，正在辨識；完成後會在這個聊天室回覆。'
STYLE = '''body{font:16px system-ui,sans-serif;margin:32px auto;max-width:1100px;padding:0 20px;background:#f3f5f7;color:#17202a}article,section{background:white;padding:20px;margin:18px 0;border-radius:12px}img{max-width:100%;height:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#155bc0}table{border-collapse:collapse;width:100%}td,th{padding:10px;border-bottom:1px solid #ddd;text-align:left}.error{color:#a11}small{color:#536171}'''


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover(data_dir, start, end, *, require_complete=True):
    found = {}
    for path in data_dir.iterdir():
        match = re.fullmatch(r'9[:_\-](\d{1,2})\.(jpg|jpeg|png|webp)', path.name, re.I)
        if match and start <= int(match[1]) <= end:
            day = int(match[1])
            if day in found:
                raise ValueError(f'9/{day} 有重複照片，請用明確檔案參數選擇：{found[day]}、{path}')
            found[day] = path
    missing = [day for day in range(start, end + 1) if day not in found]
    if missing and require_complete:
        raise ValueError(f'缺少照片：{missing}')
    if not found:
        raise ValueError(f'{data_dir} 在 9/{start}–9/{end} 沒有可用照片')
    return [found[day] for day in sorted(found)]


def recovered_usage(pipeline, model):
    """Include billed responses even if a later stage failed."""
    from types import SimpleNamespace
    records = []
    for path in sorted((pipeline / 'api').glob('*/*.json')):
        saved = json.loads(path.read_text()); raw = saved['response']
        response = SimpleNamespace(id=raw['id'], model=raw.get('model'),
                                   service_tier=raw.get('service_tier'),
                                   usage=SimpleNamespace(model_dump=lambda raw=raw: raw.get('usage')) if raw.get('usage') else None)
        records.append(usage_record(response, model, saved['elapsed_seconds']))
    return summarize_usage(records)


def render_conversation(folder, messages, status):
    blocks = []
    for msg in messages:
        role = '使用者' if msg['role'] == 'user' else '機器人'
        content = (f'<img loading="lazy" src="{html.escape(msg["path"], quote=True)}">'
                   if msg['type'] == 'image' else f'<pre>{html.escape(msg["text"])}</pre>')
        blocks.append(f'<article><small>{role} · {html.escape(msg["at"])}</small>{content}</article>')
    page = f'<!doctype html><meta charset="utf-8"><title>LINE 模擬紀錄</title><style>{STYLE}</style><h1>LINE 模擬紀錄</h1><p>狀態：{html.escape(status)} · <a href="../index.html">返回總覽</a></p><p>完成表示流程執行成功，不代表每個字皆辨識正確。</p>' + ''.join(blocks)
    grid = folder / 'pipeline/grid/01-grid-debug.jpg'
    if grid.exists():
        page += '<details><summary>格線與實際模型輸入</summary><a href="pipeline/grid/cells.md">逐格索引</a><img src="pipeline/grid/01-grid-debug.jpg"><img loading="lazy" src="pipeline/grid/02-cell-contact-sheet.jpg"></details>'
    elif (folder / 'pipeline/registration-preview.png').exists():
        page += '<details><summary>定位檢查前的校正圖（格線未通過）</summary><img src="pipeline/registration-preview.png"></details>'
    request_path = folder / 'request.json'
    if request_path.exists():
        timing = json.loads(request_path.read_text(encoding='utf-8')).get('timing', {})
        if timing:
            rows = ''.join(
                f'<tr><td>{html.escape(name)}</td><td>{seconds:.4f} 秒</td></tr>'
                for name, seconds in timing.items()
            )
            page += '<section><h2>各階段處理時間</h2><table><tr><th>階段</th><th>耗時</th></tr>' + rows + '</table></section>'
    (folder / 'conversation.html').write_text(page, encoding='utf-8')


async def simulate_one(source, folder):
    simulation_started = time.monotonic()
    folder.mkdir()
    source_copy = folder / ('input' + source.suffix.lower())
    shutil.copy2(source, source_copy)
    now = lambda: datetime.now(timezone.utc).isoformat()
    messages = [dict(role='user', type='image', path=source_copy.name, at=now()),
                dict(role='assistant', type='text', text=ACK, at=now())]
    request = dict(source=str(source.resolve()), sha256=sha256(source), started_at=now(),
                   model=os.getenv('OPENAI_MODEL', 'gpt-5.6-luna'), status='running')
    write_json(folder / 'request.json', request)
    write_json(folder / 'conversation.json', messages)
    pipeline = folder / 'pipeline'
    try:
        content_type = mimetypes.guess_type(source.name)[0]
        stage_started = time.monotonic()
        corrected = await asyncio.to_thread(
            create_corrected_schedule_preview,
            source.read_bytes(),
            content_type=content_type,
        )
        preview_seconds = time.monotonic() - stage_started
        corrected_name = str(corrected['filename'])
        (folder / corrected_name).write_bytes(corrected['content'])
        messages.append(dict(role='assistant', type='image', path=corrected_name, at=now()))
        write_json(folder / 'conversation.json', messages)
        stage_started = time.monotonic()
        result = await analyze_schedule_image(source.read_bytes(), filename=source.name,
            content_type=content_type, all_employees=True,
            include_crop_images=True, use_final_markdown=True, artifacts_dir=pipeline,
            compact_artifacts=True)
        analysis_seconds = time.monotonic() - stage_started
        response_started = time.monotonic()
        for employee in result['employees']:
            text = format_line_report(employee)
            (folder / f'{employee["employee_code"]}.txt').write_text(text, encoding='utf-8')
            messages.append(dict(role='assistant', type='text', text=text, at=now()))
            crop = employee.pop('crop_image', None)
            if crop:
                name = f'{employee["employee_code"]}.jpg'
                (folder / name).write_bytes(crop['content'])
                employee['crop_image'] = {'path': name, 'filename': crop['filename']}
                messages.append(dict(role='assistant', type='image', path=name, at=now()))
        messages.append(dict(role='assistant', type='text', text=format_line_total_price(result), at=now()))
        write_json(folder / 'result.json', result)
        response_seconds = time.monotonic() - response_started
        request['timing'] = {
            'corrected_preview_seconds': round(preview_seconds, 4),
            **result.get('timing', {}).get('stages', {}),
            'schedule_analysis_seconds': round(analysis_seconds, 4),
            'response_serialization_seconds': round(response_seconds, 4),
            'simulation_total_seconds': round(time.monotonic() - simulation_started, 4),
        }
        request.update(status='completed', employee_count=len(result['employees']), usage=result['usage'])
    except Exception as exc:
        error_text = str(exc) if isinstance(exc, ScheduleUploadError) else format_line_error(exc)
        messages.append(dict(role='assistant', type='text', text=error_text, at=now()))
        request.update(status='failed', error_type=type(exc).__name__, error=str(exc))
        (folder / 'error.txt').write_text(traceback.format_exc(), encoding='utf-8')
    request['finished_at'] = now()
    request['observed_usage'] = recovered_usage(pipeline, request['model'])
    write_json(folder / 'request.json', request)
    write_json(folder / 'conversation.json', messages)
    text_log = []
    for msg in messages:
        text_log.append(f'[{msg["at"]}] {msg["role"]}\n' + (msg['text'] if msg['type'] == 'text' else f'[圖片] {msg["path"]}'))
    (folder / 'conversation.txt').write_text('\n\n'.join(text_log), encoding='utf-8')
    render_conversation(folder, messages, request['status'])
    return dict(folder=folder.name, **request)


def write_index(run, results):
    write_json(run / 'summary.json', results)
    rows = []
    for item in results:
        elapsed = item.get('timing', {}).get('simulation_total_seconds')
        elapsed_text = f'{elapsed:.2f} 秒' if isinstance(elapsed, (int, float)) else '—'
        rows.append(f'<tr><td><a href="{item["folder"]}/conversation.html">{html.escape(Path(item["source"]).name)}</a></td><td>{item["status"]}</td><td>{elapsed_text}</td></tr>')
    page = f'<!doctype html><meta charset="utf-8"><title>LINE 模擬測試總覽</title><style>{STYLE}</style><h1>LINE 模擬測試總覽</h1><p>每次問答各自保存原圖、過程檔、文字與圖片。未發送到 LINE。</p><p>completed 僅代表流程完成；仍需目視核對辨識與格線。</p><table><tr><th>照片／紀錄</th><th>狀態</th><th>總時間</th></tr>' + ''.join(rows) + '</table>'
    completed = sum(item['status'] == 'completed' for item in results)
    total_seconds = sum(
        item.get('timing', {}).get('simulation_total_seconds', 0)
        for item in results
    )
    page += f'<section>流程完成 {completed} / {len(results)}；累計總時間 {total_seconds:.2f} 秒。</section>'
    (run / 'index.html').write_text(page, encoding='utf-8')
    report = ['# LINE 模擬測試紀錄', '',
              f'流程完成 {completed}/{len(results)}；累計總時間 {total_seconds:.2f} 秒。', '',
              'completed 只表示流程成功，不代表格線或每個字已人工驗證。', '',
              '| 照片 | 狀態 | 對話紀錄 | 錯誤 |', '|---|---|---|---|']
    for item in results:
        error = item.get('error', '').replace('|', '/').replace('\n', ' ')
        report.append(f'| {Path(item["source"]).name} | {item["status"]} | [{item["folder"]}]({item["folder"]}/conversation.html) | {error} |')
    (run / 'report.md').write_text('\n'.join(report) + '\n', encoding='utf-8')


async def main_async(args):
    sources = ([Path(x).resolve() for x in args.images] if args.images else
               discover(Path(args.data_dir), args.from_day, args.to_day,
                        require_complete=args.require_complete_range))
    if any(not p.is_file() for p in sources):
        raise ValueError('輸入照片不存在')
    run = Path(args.output_root) / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8])
    run.mkdir(parents=True, exist_ok=False)
    config = _line_phase1_template()
    shutil.copy2(config, run / 'template.json')
    files = ['simulate_line.py','line_bot.py','backend/schedule_service.py','backend/phase1_cell_pipeline.py','backend/phase1_multilabel_compare.py','backend/batch_reader.py','backend/preprocessing.py','backend/api_usage.py','schedule_reader.py','requirements.txt']
    snapshot = run / 'code';snapshot.mkdir()
    for file in files:
        target = snapshot / file;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT / file,target)
    write_json(run / 'manifest.json', dict(created_at=datetime.now(timezone.utc).isoformat(),
        model=os.getenv('OPENAI_MODEL','gpt-5.6-luna'), inner_fraction=PRODUCTION_INNER_FRACTION,
        template_sha256=sha256(config), sources=[{'path':str(p),'sha256':sha256(p)} for p in sources],
        code_sha256={file:sha256(ROOT/file) for file in files}))
    write_json(run / 'prompts.json', dict(cells=MULTILABEL_INSTRUCTIONS,text=MARKDOWN_INSTRUCTIONS,text_review=MARKDOWN_REVIEW_INSTRUCTIONS))
    print(f'RUN_DIR={run.resolve()}', flush=True)
    results=[]
    write_index(run, results)
    semaphore = asyncio.Semaphore(args.jobs)
    async def run_one(index, source):
        async with semaphore:
            print(f'[{index}/{len(sources)}] {source.name} started',flush=True)
            result = await simulate_one(source, run / f'{index:02d}-{source.stem.replace(":","_")}')
            results.append(result)
            results.sort(key=lambda item:item['folder'])
            write_index(run,results)
            print(f'[{index}/{len(sources)}] {source.name} {result["status"]} {result.get("error","")}',flush=True)
    await asyncio.gather(*(run_one(index,source) for index,source in enumerate(sources,1)))
    print(f'INDEX={run.resolve() / "index.html"}',flush=True)
    return 1 if any(item['status']=='failed' for item in results) else 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('images',nargs='*',help='指定照片；省略時掃描 data 的日期範圍')
    parser.add_argument('--data-dir',default=str(ROOT/'data/schedules'))
    parser.add_argument('--from-day',type=int,default=1)
    parser.add_argument('--to-day',type=int,default=30)
    parser.add_argument('--require-complete-range',action='store_true',
                        help='指定日期範圍有缺圖時停止；預設只處理實際存在的照片')
    parser.add_argument('--jobs',type=int,choices=range(1,4),default=1,help='同時測試照片數（預設依序）')
    parser.add_argument('--output-root',default=str(ROOT/'data/line-simulations'))
    args=parser.parse_args()
    if not 1 <= args.from_day <= args.to_day <= 30:
        parser.error('日期範圍必須為 1–30 且起日不晚於迄日')
    return asyncio.run(main_async(args))


if __name__ == '__main__':
    raise SystemExit(main())
