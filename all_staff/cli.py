from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_DATASET = ROOT/"data/all_staff"
CONTEXT_DATASET = ROOT/"data/all_staff_ctx"
LINE_MODEL = ROOT/"models/all_staff/line-v2/recognizer.pt"


def default_dataset() -> Path:
    """The context dataset once it holds cells (V2 lines), else the legacy one."""
    manifest = CONTEXT_DATASET/"annotations.jsonl"
    return CONTEXT_DATASET if manifest.is_file() and manifest.stat().st_size else LEGACY_DATASET


def default_schedules() -> Path:
    """data/schedules of this checkout, or of the main checkout when this is a git worktree.

    Photos are git-ignored, so a ``git worktree`` (e.g. codex/all-staff-ocr) has none;
    its ``.git`` file points at ``<main>/.git/worktrees/<name>``.
    """
    local = ROOT/"data/schedules"
    if local.is_dir() and any(local.iterdir()):
        return local
    marker = ROOT/".git"
    if marker.is_file():
        text = marker.read_text(encoding="utf-8").strip()
        if text.startswith("gitdir:") and "/.git/" in text.replace("\\", "/"):
            main = Path(text.split(":", 1)[1].strip().replace("\\", "/").split("/.git/")[0])
            if (main/"data/schedules").is_dir():
                return main/"data/schedules"
    return local


def default_recognizer() -> Path:
    """The most recently trained V2 line model (models/all_staff/line-*/recognizer.pt)."""
    trained = sorted((ROOT/"models/all_staff").glob("line-*/recognizer.pt"), key=lambda p: p.stat().st_mtime)
    return trained[-1] if trained else ROOT/"models/all_staff/recognizer.pt"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="隔離的全員工代號偵測＋CTC OCR")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare"); prepare.add_argument("--schedules", type=Path, default=None); prepare.add_argument("--dataset", type=Path, default=LEGACY_DATASET); prepare.add_argument("--template", type=Path, default=ROOT/"configs/phase1_roster_template.local.json")
    template = sub.add_parser("make-template"); template.add_argument("--source", type=Path, default=ROOT/"configs/phase1_roster_template.local.json"); template.add_argument("--output", type=Path, default=ROOT/"configs/all_staff_roster_template.local.json")
    automatic = sub.add_parser("auto-label"); automatic.add_argument("--dataset", type=Path, default=None); automatic.add_argument("--workers", type=int); automatic.add_argument("--limit", type=int); automatic.add_argument("--replace-proposals", action="store_true")
    model_label = sub.add_parser("model-label"); model_label.add_argument("--dataset", type=Path, default=None); model_label.add_argument("--detector", type=Path, default=ROOT/"models/all_staff/detector.pt"); model_label.add_argument("--recognizer", type=Path, default=None); model_label.add_argument("--roster", type=Path, default=ROOT/"configs/all_staff_roster.local.json"); model_label.add_argument("--limit", type=int); model_label.add_argument("--keep-proposals", action="store_true")
    label = sub.add_parser("label"); label.add_argument("--dataset", type=Path, default=None); label.add_argument("--roster", type=Path, default=ROOT/"configs/all_staff_roster.local.json"); label.add_argument("--host", default="127.0.0.1"); label.add_argument("--port", type=int, default=8775); label.add_argument("--no-browser", action="store_true")
    for name in ("train-detector", "train-recognizer"):
        item = sub.add_parser(name); item.add_argument("--dataset", type=Path, default=None); item.add_argument("--epochs", type=int, default=30 if name.endswith("detector") else 40); item.add_argument("--batch-size", type=int, default=16 if name.endswith("detector") else 32); item.add_argument("--learning-rate", type=float, default=3e-4); item.add_argument("--output", type=Path, default=ROOT/"models/all_staff"/("detector.pt" if name.endswith("detector") else "recognizer.pt"))
    analyze = sub.add_parser("analyze"); analyze.add_argument("image", type=Path); analyze.add_argument("--template", type=Path, default=ROOT/"configs/all_staff_roster_template.local.json"); analyze.add_argument("--detector", type=Path, default=ROOT/"models/all_staff/detector.pt"); analyze.add_argument("--recognizer", type=Path, default=None); analyze.add_argument("--roster", type=Path, default=ROOT/"configs/all_staff_roster.local.json"); analyze.add_argument("--output", type=Path)
    evaluation = sub.add_parser("evaluate"); evaluation.add_argument("--dataset", type=Path, default=None); evaluation.add_argument("--detector", type=Path, default=ROOT/"models/all_staff/detector.pt"); evaluation.add_argument("--recognizer", type=Path, default=None); evaluation.add_argument("--roster", type=Path, default=ROOT/"configs/all_staff_roster.local.json"); evaluation.add_argument("--split", default="test", help="train、val、test，或以逗號合併如 val,test")
    status = sub.add_parser("status"); status.add_argument("--dataset", type=Path, default=None)
    ctx = sub.add_parser("prepare-context", help="裁切含鄰格邊界的 context 格子並搬移既有標註")
    ctx.add_argument("--schedules", type=Path, default=None)
    ctx.add_argument("--source", type=Path, default=LEGACY_DATASET)
    ctx.add_argument("--dataset", type=Path, default=CONTEXT_DATASET)
    ctx.add_argument("--template", type=Path, default=ROOT/"configs/all_staff_roster_template.local.json")
    ctx.add_argument("--fx", type=float, default=.22); ctx.add_argument("--fy", type=float, default=.12)
    ctx.add_argument("--method", choices=("tracked", "v1"), default="tracked",
                     help="tracked：V2 格線追蹤＋逐格透視校正（預設）；v1：沿用舊切格")
    viewer = sub.add_parser("viewer", help="產生靜態網頁，檢視 V2 line model 的整表推論")
    viewer.add_argument("--dataset", type=Path, default=None)
    viewer.add_argument("--recognizer", type=Path, default=None)
    viewer.add_argument("--roster", type=Path, default=ROOT/"configs/all_staff_roster.local.json")
    viewer.add_argument("--schedules", type=Path, default=None)
    viewer.add_argument("--output", type=Path, default=ROOT/"reports/all_staff_viewer")
    viewer.add_argument("--split", help="只輸出 train／val／test（可逗號合併）")
    viewer.add_argument("--sheet", action="append", help="source_group 前綴，可重複")
    viewer.add_argument("--limit-sheets", type=int)
    demo = sub.add_parser("demo", help="本機上傳模擬：上傳一張班表照片，看 V2 辨識結果")
    demo.add_argument("--recognizer", type=Path, default=None)
    demo.add_argument("--roster", type=Path, default=ROOT/"configs/all_staff_roster.local.json")
    demo.add_argument("--template", type=Path, default=ROOT/"configs/all_staff_roster_template.local.json")
    demo.add_argument("--runs", type=Path, default=ROOT/"reports/all_staff_demo/runs")
    demo.add_argument("--host", default="127.0.0.1"); demo.add_argument("--port", type=int, default=8776)
    demo.add_argument("--device", default="cpu"); demo.add_argument("--no-browser", action="store_true")
    lines = sub.add_parser("train-lines", help="合成資料預訓練＋真實資料微調的 V2 line recognizer")
    lines.add_argument("--dataset", type=Path, default=None)
    lines.add_argument("--output", type=Path, default=LINE_MODEL)
    lines.add_argument("--synth-samples", type=int, default=60000)
    lines.add_argument("--pretrain-epochs", type=int, default=10)
    lines.add_argument("--finetune-epochs", type=int, default=8)
    lines.add_argument("--real-repeat", type=int, default=6)
    lines.add_argument("--batch-size", type=int, default=128)
    lines.add_argument("--learning-rate", type=float, default=1e-3)
    lines.add_argument("--workers", type=int, default=4)
    lines.add_argument("--device", help="cuda、mps 或 cpu；預設有 CUDA 用 CUDA，否則 CPU")
    lines.add_argument("--init", type=Path, help="從既有 checkpoint 接續（可配合較少的預訓練輪數）")
    lines.add_argument("--monitor", action="store_true", help="同時開啟訓練監控網頁")
    lines.add_argument("--port", type=int, default=8778); lines.add_argument("--no-browser", action="store_true")
    sim = sub.add_parser("line-sim", help="本機 LINE 模擬器：用 V2 模型測試班表機器人")
    sim.add_argument("--recognizer", type=Path, default=None)
    sim.add_argument("--roster", type=Path, default=ROOT/"configs/all_staff_roster.local.json")
    sim.add_argument("--template", type=Path, default=ROOT/"configs/all_staff_roster_template.local.json")
    sim.add_argument("--folder", type=Path, default=ROOT/"reports/line_sim")
    sim.add_argument("--host", default="127.0.0.1"); sim.add_argument("--port", type=int, default=8779)
    sim.add_argument("--no-browser", action="store_true")
    sim.add_argument("--admin", action="append", default=[], metavar="USER_ID", help="模擬器裡當管理員的使用者（例如 U_ming，可重複）：群組每張照片的辨識結果回報給管理員（預設放進收件匣）；加 --push 時，日期讀不到的群組班表先問管理員，確認後才通知")
    sim.add_argument("--push", action="store_true", help="模擬開啟推播（LINE_V2_PUSH=1）：辨識回報、姓名申請等直接私訊。預設和正式環境一樣不推播，放進收件匣")
    sim.add_argument("--notify", action="store_true", help="模擬開啟自動通知（LINE_V2_NOTIFY=1，要配合 --push）：群組有新班表時私訊已開通知的人。預設關閉")
    bot = sub.add_parser("line-bot", help="正式 LINE webhook（與 line-sim 相同邏輯）")
    bot.add_argument("--recognizer", type=Path,
                     default=Path(__import__("os").environ["LINE_V2_RECOGNIZER"]) if __import__("os").getenv("LINE_V2_RECOGNIZER") else None)
    bot.add_argument("--roster", type=Path, default=ROOT/"configs/all_staff_roster.local.json")
    bot.add_argument("--template", type=Path, default=ROOT/"configs/all_staff_roster_template.local.json")
    bot.add_argument("--folder", type=Path, default=ROOT/"data/line_v2")
    bot.add_argument("--host", default="0.0.0.0")
    bot.add_argument("--port", type=int, default=int(__import__("os").getenv("LINE_V2_PORT", "8081")))
    regrid = sub.add_parser("reanalyze", help="把 LINE 機器人已存的班表用目前的格線規則重讀一次（最後四欄、忽略浮動視窗），不必重傳照片")
    regrid.add_argument("--recognizer", type=Path, default=None)
    regrid.add_argument("--roster", type=Path, default=ROOT/"configs/all_staff_roster.local.json")
    regrid.add_argument("--template", type=Path, default=ROOT/"configs/all_staff_roster_template.local.json")
    regrid.add_argument("--folder", type=Path, default=ROOT/"data/line_v2")
    regrid.add_argument("--run", action="append", help="只重讀這一筆（runs/ 底下的資料夾名稱），可重複")
    regrid.add_argument("--limit", type=int, help="最多重讀幾筆（由新到舊）")
    regrid.add_argument("--force", action="store_true", help="已是最新格線的也重讀；先前失敗的也再試")
    check = sub.add_parser("line-check", help="檢查 LINE 機器人存的班表：日期、姓名對照表、「我是」")
    check.add_argument("--folder", type=Path, default=ROOT/"data/line_v2")
    check.add_argument("--template", type=Path, default=ROOT/"configs/all_staff_roster_template.local.json")
    check.add_argument("--reread", action="store_true", help="每張班表都從照片重讀姓名對照表")
    check.add_argument("--restore-dates", action="store_true", help="把被「日期」改過的班表改回照片上辨識到的日期")
    check.add_argument("--run", help="只檢查這一筆（runs/ 底下的資料夾名稱）")
    menu = sub.add_parser("line-menu", help="上傳 LINE 1:1 聊天室下方的圖文選單：所有人、幹部、管理員三種，並幫幹部和管理員換上")
    menu.add_argument("--dry-run", action="store_true", help="只重畫三張選單圖片（assets/line_rich_menu*.png），不上傳")
    menu.add_argument("--redraw", action="store_true", help="上傳前重畫圖片（需要中文字型）")
    menu.add_argument("--folder", type=Path, default=ROOT/"data/line_v2",
                      help="機器人的資料夾：裡面的 bot.sqlite3 有幹部名單和誰換了選單")
    mon = sub.add_parser("monitor", help="監控 train-lines 的進度（讀 <output>.json）")
    mon.add_argument("--run", type=Path, default=None, help="進度檔，預設 models/all_staff/line-v2/recognizer.json")
    mon.add_argument("--port", type=int, default=8778); mon.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if getattr(args, "dataset", "unset") is None: args.dataset = default_dataset()
    if getattr(args, "recognizer", "unset") is None: args.recognizer = default_recognizer()
    if getattr(args, "schedules", "unset") is None: args.schedules = default_schedules()
    if args.command == "make-template":
        from .dataset import full_grid_template
        result = full_grid_template(args.source, args.output)
    elif args.command == "auto-label":
        from .auto_label import auto_label
        result = auto_label(args.dataset, workers=args.workers, limit=args.limit,
                            replace_proposals=args.replace_proposals)
    elif args.command == "prepare-context":
        import shutil, time as _time
        from .context import migrate_annotations, prepare_context
        print(f"照片來源：{args.schedules}", flush=True)
        backup = None
        if (args.dataset/"annotations.jsonl").is_file():
            # never re-crop in place: old boxes belong to the old crops
            backup = args.dataset.with_name(args.dataset.name + _time.strftime(".bak-%Y%m%d-%H%M%S"))
            shutil.move(str(args.dataset), backup)
            print(f"舊資料集已移到 {backup}", flush=True)
        prepared = prepare_context(args.schedules, args.dataset, args.template, fx=args.fx, fy=args.fy,
                                   method=args.method)
        migrations = {}
        if backup is not None:  # newest human work first
            migrations[str(backup)] = migrate_annotations(backup, args.dataset)
        migrations[str(args.source)] = migrate_annotations(args.source, args.dataset, overwrite=False)
        for report in migrations.values():
            report["weak_matches"] = report["weak_matches"][:20]; report["moved"] = report["moved"][:20]
        result = {"prepare": {k: v for k, v in prepared.items() if k not in ("fallbacks",)},
                  "migration": migrations, "backup": str(backup) if backup else None}
    elif args.command == "demo":
        from .demo import DemoApp, serve
        from .inference import is_line_checkpoint
        if not is_line_checkpoint(args.recognizer):
            raise SystemExit(f"{args.recognizer} 不是 V2 line recognizer；請先 train-lines 或指定 --recognizer")
        if not args.template.is_file():
            from .dataset import full_grid_template
            full_grid_template(ROOT/"configs/phase1_roster_template.local.json", args.template)
        app = DemoApp(ROOT, args.recognizer, roster=args.roster, template=args.template,
                      runs=args.runs, device=args.device)
        serve(app, args.host, args.port, open_browser=not args.no_browser); return 0
    elif args.command == "viewer":
        from .viewer import build_viewer
        result = build_viewer(args.dataset, args.recognizer, args.output, roster=args.roster,
                              schedules=args.schedules,
                              splits=set(args.split.split(",")) if args.split else None,
                              sheets=args.sheet, limit_sheets=args.limit_sheets)
    elif args.command == "line-sim":
        from .demo import DemoApp
        from .inference import is_line_checkpoint
        from .line_sim import serve as serve_sim
        if not is_line_checkpoint(args.recognizer):
            raise SystemExit(f"{args.recognizer} 不是 V2 line recognizer；請用 --recognizer 指定")
        if not args.template.is_file():
            from .dataset import full_grid_template
            full_grid_template(ROOT/"configs/phase1_roster_template.local.json", args.template)
        print(f"使用模型：{args.recognizer}", flush=True)
        analyzer = DemoApp(ROOT, args.recognizer, roster=args.roster, template=args.template,
                           runs=args.folder/"runs")
        serve_sim(analyzer, args.folder, args.host, args.port, open_browser=not args.no_browser,
                  admins=tuple(args.admin), notify=args.notify, push=args.push); return 0
    elif args.command == "line-bot":
        import uvicorn
        from dotenv import load_dotenv
        from .line_webhook import create_app
        from .inference import is_line_checkpoint
        load_dotenv(ROOT/".env")
        if not is_line_checkpoint(args.recognizer):
            raise SystemExit(f"{args.recognizer} 不是 V2 line recognizer；請把模型放到 models/all_staff/line-v3/ 或設定 LINE_V2_RECOGNIZER")
        if not args.template.is_file():
            from .dataset import full_grid_template
            full_grid_template(ROOT/"configs/phase1_roster_template.local.json", args.template)
        print(f"使用模型：{args.recognizer}", flush=True)
        app = create_app(ROOT, args.recognizer, roster=args.roster, template=args.template, folder=args.folder)
        uvicorn.run(app, host=args.host, port=args.port); return 0
    elif args.command == "reanalyze":
        from .demo import DemoApp
        from .inference import is_line_checkpoint
        if not is_line_checkpoint(args.recognizer):
            raise SystemExit(f"{args.recognizer} 不是 V2 line recognizer；請用 --recognizer 指定")
        from .staff_book import live_files
        staff_names, fixed_codes = live_files(ROOT/"configs", args.folder)   # the list 幹部 edit in LINE
        app = DemoApp(ROOT, args.recognizer, roster=args.roster, template=args.template, runs=args.folder/"runs",
                      staff_names=staff_names, fixed_codes=fixed_codes)
        result = app.upgrade_runs(force=args.force, only=args.run, limit=args.limit)
    elif args.command == "line-check":
        from .roster_check import check_rosters
        from .staff_book import live_files
        staff_names, _ = live_files(ROOT/"configs", args.folder)   # the list 幹部 edit in LINE
        check_rosters(args.folder, args.template, staff_names, reread=args.reread,
                      restore_dates=args.restore_dates, only=args.run); return 0
    elif args.command == "line-menu":
        import os
        from . import rich_menu
        if args.dry_run or args.redraw:
            for role in rich_menu.ROLES:
                print(f"已產生 {rich_menu.draw(role=role)}", flush=True)
        if args.dry_run:
            return 0
        try:
            from dotenv import load_dotenv
            load_dotenv(ROOT/".env")
        except ImportError:
            pass
        token = os.getenv("LINE_V2_CHANNEL_ACCESS_TOKEN")
        if not token:
            raise SystemExit("找不到 LINE_V2_CHANNEL_ACCESS_TOKEN（.env）。")
        from .bot import RosterBot, Settings, Store
        from .line_webhook import LineTransport, admin_ids
        # New menus have new ids: the 幹部 and administrators are linked to them before the old ones are deleted
        # (that drops every link to them), with the server's own records, so it does not link them again. Without
        # those records the 幹部 would be left on the default and the server would keep naming deleted menus.
        database = args.folder/"bot.sqlite3"
        if not database.is_file():
            raise SystemExit(f"找不到 {database}（伺服器存的資料庫）。請在伺服器上、伺服器啟動過之後再執行，"
                             "或用 --folder 指到伺服器存資料的資料夾；只想重畫圖片請加 --dry-run。")
        transport = LineTransport(token)
        bot = RosterBot(None, Store(database), transport, Settings(admins=admin_ids(os.getenv("LINE_V2_ADMIN_USER_IDS"))))
        done = {}

        def relink(ids):
            transport.remember_menus({rich_menu.NAMES[role]: menu_id for role, menu_id in ids.items()})
            done.update(bot.sync_menus())
        ids = rich_menu.install(token, relink=relink)
        print(f"已設為預設選單：{ids[None]}（幹部選單 {ids['officer']}、管理員選單 {ids['admin']}）", flush=True)
        print(f"已換上：管理員選單 {done.get('admin', 0)} 位、幹部選單 {done.get('officer', 0)} 位"
              + (f"；{done['default']} 位改回一般選單" if done.get("default") else "")
              + (f"；{done['failed']} 位沒換成，請看上面的訊息" if done.get("failed") else ""), flush=True)
        bot.store.db.close()
        return 0
    elif args.command == "monitor":
        from .monitor import start
        start(args.run or LINE_MODEL.with_suffix(".json"), port=args.port,
              open_browser=not args.no_browser, background=False); return 0
    elif args.command == "train-lines":
        import shutil, time as _time
        from .context import load_context
        from .lines import train_line_recognizer
        if not load_context(args.dataset).get("fx"):
            raise SystemExit(f"{args.dataset} 不是 context 資料集；請先執行 python -m all_staff.cli prepare-context")
        if args.output.is_file():  # never lose the model the demo/viewer is using
            backup = args.output.with_name(args.output.stem + _time.strftime(".bak-%Y%m%d-%H%M%S") + args.output.suffix)
            shutil.copy2(args.output, backup)
            print(f"既有模型已備份到 {backup}", flush=True)
            if args.init and args.init.resolve() == args.output.resolve():
                args.init = backup  # the output is rewritten during training
        if args.monitor:
            from .monitor import start
            try:
                start(args.output.with_suffix(".json"), port=args.port, open_browser=not args.no_browser)
            except OSError as exc:
                print(f"監控網頁無法啟動（{exc}）；可另開終端機執行 monitor --run {args.output.with_suffix('.json')}")
        try:
            result = train_line_recognizer(args.dataset, args.output, synth_samples=args.synth_samples,
                                           pretrain_epochs=args.pretrain_epochs,
                                           finetune_epochs=args.finetune_epochs, real_repeat=args.real_repeat,
                                           batch_size=args.batch_size, learning_rate=args.learning_rate,
                                           workers=args.workers, device=args.device, init=args.init)
        except Exception:
            if not args.monitor:
                raise
            import traceback
            traceback.print_exc(); result = {"status": "failed"}
        result.pop("history", None)
        if args.monitor:  # keep the page reachable so the final result can be read
            print(json.dumps(result, ensure_ascii=False, indent=2, default=dict))
            print("訓練結束；監控網頁仍開著，按 Ctrl+C 離開。", flush=True)
            try:
                import time as _time
                while True: _time.sleep(3600)
            except KeyboardInterrupt:
                return 0
    elif args.command == "model-label":
        from .auto_label import model_auto_label
        result = model_auto_label(args.dataset, args.detector, args.recognizer, args.roster,
                                  replace_proposals=not args.keep_proposals,
                                  limit=args.limit)
    elif args.command == "prepare":
        from .dataset import prepare_cells
        from .dataset import full_grid_template
        full_template = ROOT/"configs/all_staff_roster_template.local.json"
        template = args.template
        if template.name == "phase1_roster_template.local.json":
            full_grid_template(template, full_template); template = full_template
        result = prepare_cells(args.schedules, args.dataset, template)
    elif args.command == "label":
        from .annotate import serve
        serve(args.dataset, args.host, args.port, roster=args.roster,
              open_browser=not args.no_browser); return 0
    elif args.command == "train-detector":
        from .train import train_detector
        result = train_detector(args.dataset, args.output, epochs=args.epochs, batch_size=args.batch_size, learning_rate=args.learning_rate)
    elif args.command == "train-recognizer":
        from .train import train_recognizer
        result = train_recognizer(args.dataset, args.output, epochs=args.epochs, batch_size=args.batch_size, learning_rate=args.learning_rate)
    elif args.command == "status":
        from collections import Counter
        from .schema import load_annotations
        items = load_annotations(args.dataset/"annotations.jsonl")
        reviewed = [item for item in items if item.reviewed]
        positives = [item for item in reviewed if any(
            ann.role == "personnel_code" for ann in item.annotations)]
        result = {"cells": len(items), "reviewed_cells": len(reviewed),
                  "splits": Counter(item.split for item in reviewed),
                  "reviewed_positive_cells": len(positives),
                  "positive_splits": Counter(item.split for item in positives),
                  "positive_source_photos": len({item.source_group for item in positives}),
                  "codes": Counter(ann.text for item in reviewed for ann in item.annotations if ann.role == "personnel_code")}
    elif args.command == "evaluate":
        from .context import load_context
        from .evaluate import evaluate
        from .inference import load_engine
        engine = load_engine(args.recognizer, detector=args.detector, roster=args.roster,
                             context=load_context(args.dataset))
        result = evaluate(args.dataset, engine, split=args.split)
    else:
        from .context import prepare_context
        from .dataset import prepare_cells
        from .inference import is_line_checkpoint, load_engine
        line_model = is_line_checkpoint(args.recognizer)
        with tempfile.TemporaryDirectory(prefix="all-staff-") as folder:
            temporary = Path(folder)
            if line_model:
                import torch
                context = torch.load(args.recognizer, map_location="cpu", weights_only=False)["context"]
                prepare_context(args.image.parent, temporary, args.template,
                                fx=context["fx"], fy=context["fy"], only_sources=[args.image.resolve()])
            else:
                prepare_cells(args.image.parent, temporary, args.template,
                              only_sources=[args.image.resolve()])
            import hashlib
            group = hashlib.sha256(args.image.read_bytes()).hexdigest()[:16]
            cells = temporary/"images"
            selected = temporary/"selected"; selected.mkdir()
            for path in cells.glob(f"{group}_*.png"): path.replace(selected/path.name)
            engine = load_engine(args.recognizer, detector=args.detector, roster=args.roster)
            result = engine.analyze_cells(selected)
        if args.output:
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=dict))
    return 0


if __name__ == "__main__": raise SystemExit(main())
