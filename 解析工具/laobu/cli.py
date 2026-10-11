import argparse
import csv
import json
from pathlib import Path
from .common import CorpusLock, ROOT, now, read_json, save_json, fingerprint
from .database import Database
from .media import probe, runtime_info
from .outputs import export_corpus, publish_view
from .pipeline import run_batch

def load_config(path):
    path = Path(path).resolve()
    config = read_json(path)
    for key in ["input_dir", "output_dir", "model_dir"]:
        p = Path(config[key])
        config[key] = str((path.parent / p).resolve() if not p.is_absolute() else p.resolve())
    if config["device"] not in ["auto", "cpu", "cuda"]:
        raise ValueError("device只能是auto/cpu/cuda")
    if not (.1 <= config["sample_interval"] <= 5):
        raise ValueError("sample_interval须在0.1至5秒之间")
    roi = config.get("roi")
    if roi is not None and (len(roi) != 4 or not (0 <= roi[0] < roi[2] <= 1 and 0 <= roi[1] < roi[3] <= 1)):
        raise ValueError("roi必须为归一化[x1,y1,x2,y2]，左上小于右下")
    if not (1 <= config["cpu_threads"] <= 64 and 1 <= config["ocr_threads"] <= 16):
        raise ValueError("线程数超出范围")
    if config["retry_limit"] < 0 or config["max_gap_retry_seconds"] <= 0:
        raise ValueError("二次识别限制无效")
    return config

def parser():
    p = argparse.ArgumentParser(description="老布本地视频语料库 V1.0；不会自动下载模型或上传数据")
    p.add_argument("--config", default=str(ROOT / "解析工具/config.json"))
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="检查依赖/内存/CUDA/模型缓存")
    scan = commands.add_parser("scan", help="递归扫描MP4，仅索引，不转写")
    scan.add_argument("--input")
    scan.add_argument("--file")
    process = commands.add_parser("process", help="小批量处理/断点恢复；必须限制数量或指定ID")
    process.add_argument("--id", action="append", dest="ids")
    process.add_argument("--title", action="append", help="筛选标题，可重复；须搭配--limit")
    process.add_argument("--limit", type=int)
    process.add_argument("--retry-failed", action="store_true")
    process.add_argument("--resume", action="store_true", help="续跑未完成、已中断及失败视频，仍跳过已完成")
    process.add_argument("--force", action="store_true", help="指定ID重跑，保留所有历史版本和核对记录")
    process.add_argument("--all-confirmed", action="store_true", help="用户明确批准全量运行后才使用")
    commands.add_parser("status", help="全库和最近任务状态")
    search = commands.add_parser("search", help="检索ASR和OCR，支持短中文关键词")
    search.add_argument("query")
    export = commands.add_parser("export", help="导出全库索引和已完成研究包")
    export.add_argument("--batch-size", type=int, default=10)
    metadata = commands.add_parser("metadata", help="人工修正标题、系列及集数")
    metadata.add_argument("id")
    metadata.add_argument("--title")
    metadata.add_argument("--series", help="空字符串表示清除系列")
    metadata.add_argument("--episode", type=int)
    metadata.add_argument("--total", type=int)
    review = commands.add_parser("review", help="附加人工核对记录，需要核对者、原文依据和时间区间")
    review.add_argument("review_id")
    review.add_argument("--author", required=True)
    review.add_argument("--status", choices=["pending", "resolved", "ignored"], required=True)
    review.add_argument("--text", required=True)
    review.add_argument("--evidence", required=True, help="回听/画面/依据说明，不能只写猜测")
    review.add_argument("--apply-transcript", action="store_true", help="明确将核对文本用于候选口播；只对resolved和完整匹配的时间范围应用")
    legacy = commands.add_parser("import-legacy", help="只读导入旧核对台账，独立保存不覆盖")
    legacy.add_argument("--directory", default=str(ROOT / "01_转写结果"))
    refresh = commands.add_parser("refresh", help="从已提交版本修复/更新根目录展示文件")
    refresh.add_argument("--id")
    rebuild = commands.add_parser("realign", help="仅用已有ASR/OCR重建对齐，保存新版本，不重做识别")
    rebuild.add_argument("--id", action="append", required=True, dest="ids")
    return p

def time_value(text):
    h, m, s = text.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)

def import_legacy(db, folder):
    sources = [("赌徒之死", Path(folder) / "四集字幕核对清单.csv"), ("皇家马戏团", Path(folder) / "皇家马戏团_字幕核对清单.csv")]
    hash_map = {}
    old_process = Path(folder) / "过程资料"
    if (old_process / "视频检查.json").exists():
        for item in read_json(old_process / "视频检查.json"):
            hash_map[("赌徒之死",item['episode'])] = item['sha256']
    if (old_process / "皇家马戏团/视频检查.json").exists():
        hash_map[("皇家马戏团",None)] = read_json(old_process / "皇家马戏团/视频检查.json")['sha256']
    count = pending = 0
    for title, source in sources:
        if not source.is_file():
            print(f"旧台账不存在，未假定其已导入：{source}")
            continue
        with source.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        with db.db:
            for number, record in enumerate(rows, 1):
                episode = int(record['集数']) if title == '赌徒之死' else None
                digest = hash_map.get((title,episode))
                if digest:
                    videos = db.rows("SELECT * FROM videos WHERE sha256=?",(digest,))
                else:
                    videos = db.rows("SELECT * FROM videos WHERE title=?", (title,))
                    if title == "赌徒之死":
                        videos = [v for v in videos if v["episode"] == episode]
                if len(videos) != 1:
                    raise ValueError(f"旧台账映射不唯一：{title} {record.get('集数')}")
                vid = videos[0]["id"]
                source_key = str(source.resolve())
                db.db.execute("INSERT OR IGNORE INTO legacy_records VALUES (?,?,?,?)", (vid, source_key, number, json.dumps(record, ensure_ascii=False)))
                count += 1
                doubt = record.get("存疑说明", "") or record.get("待核说明", "")
                if doubt or "待" in record.get("核对状态", ""):
                    start, end = time_value(record["开始"]), time_value(record["结束"])
                    rid = "LEGACY-" + fingerprint({"source": source_key, "row": number, "video": vid})
                    text = doubt or record.get("核对状态", "")
                    db.db.execute("INSERT OR IGNORE INTO review_items VALUES (?,?,?,?,?,?,?,?,?,?)", (rid, vid, "legacy", start, end, "legacy_pending", "pending", text, json.dumps(record, ensure_ascii=False), now()))
                    pending += 1
    cue_count = 0
    process = Path(folder) / "过程资料"
    checks_file = process / "视频检查.json"
    if checks_file.exists():
        checks = read_json(checks_file)
        for check in checks:
            cue_file = process / f"第{check['episode']}集_导出字幕.json"
            videos = db.rows("SELECT * FROM videos WHERE sha256=?", (check["sha256"],))
            if cue_file.exists() and len(videos) == 1:
                cue_count += import_cues(db, videos[0], cue_file, check)
    check_file = process / "皇家马戏团/视频检查.json"
    cue_file = process / "皇家马戏团/导出字幕.json"
    if check_file.exists() and cue_file.exists():
        check = read_json(check_file)
        videos = db.rows("SELECT * FROM videos WHERE sha256=?", (check["sha256"],))
        if len(videos) == 1:
            cue_count += import_cues(db, videos[0], cue_file, check)
    for v in db.rows("SELECT id FROM videos WHERE current_run IS NOT NULL"):
        publish_view(db, v["id"])
    return {"legacy_rows_read": count, "pending_rows_read": pending, "legacy_candidate_cues": cue_count, "note": "幂等导入；原视频SHA匹配才保留旧候选口播；旧画面核对不等同人工听核，原文件未修改"}

def import_cues(db, video, cue_file, check):
    cues = read_json(cue_file)
    with db.db:
        for index, cue in enumerate(cues, 1):
            data = {"record_type": "legacy_cue", "video_sha256": check["sha256"], "cue": cue, "scope": "此前Work机器识别/重点画面核对工作稿，未人工逐句听核"}
            db.db.execute("INSERT OR IGNORE INTO legacy_records VALUES (?,?,?,?)", (video["id"], str(cue_file.resolve()), index, json.dumps(data, ensure_ascii=False)))
    return len(cues)

def main(argv=None):
    args = parser().parse_args(argv)
    config = load_config(args.config)
    if args.command == "doctor":
        info = runtime_info()
        info["model_cache_present"] = bool(list(Path(config["model_dir"]).glob("**/model.bin")))
        save_json(Path(config["output_dir"]) / "运行日志/environment.json", info)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0 if info["model_cache_present"] and all(x != "missing" for x in info["packages"].values()) else 1
    if args.command in ["status", "search"]:
        db = Database(config["output_dir"])
        if args.command == "status":
            result = {"counts": db.rows("SELECT status,COUNT(*) AS count FROM videos GROUP BY status"), "jobs": db.rows("SELECT id,video_id,status,stage,started_at,finished_at,elapsed_seconds,error FROM processing_jobs ORDER BY started_at DESC LIMIT 15"), "pending_reviews": db.db.execute("SELECT COUNT(*) FROM review_items WHERE status='pending'").fetchone()[0]}
        else:
            result = db.search(args.query)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        db.db.close()
        return 0
    with CorpusLock(Path(config["output_dir"]) / "数据库"):
        db = Database(config["output_dir"])
        db.recover()
        if args.command == "scan":
            result = db.scan(args.input or config["input_dir"], probe, args.file)
            save_json(db.output / "运行日志/last_scan.json", result)
            print(json.dumps({"files": len(result), "new": sum(not r.get('duplicate',False) and not r.get('error') for r in result), "duplicates": sum(r.get('duplicate',False) for r in result), "errors": [r for r in result if r.get('error')]}, ensure_ascii=False, indent=2))
            return 1 if any(r.get("error") for r in result) else 0
        if args.command == "process":
            if args.force and not args.ids:
                raise ValueError("强制重跑必须显式指定--id，禁止隐式全库覆盖")
            if not args.ids and not args.limit and not args.all_confirmed:
                raise ValueError("必须指定--limit或--id；未经用户批准不要使用--all-confirmed")
            if args.limit is not None and args.limit <= 0:
                raise ValueError("--limit必须大于0")
            videos = db.rows("SELECT * FROM videos ORDER BY id")
            if args.ids:
                unknown = set(args.ids) - {v["id"] for v in videos}
                if unknown:
                    raise ValueError(f"未知ID：{sorted(unknown)}")
                videos = [v for v in videos if v["id"] in args.ids]
            if args.title:
                videos = [v for v in videos if any(t in v["title"] for t in args.title)]
            if not args.force:
                statuses = ["failed"] if args.retry_failed else ["pending", "interrupted", "failed"] if args.resume else ["pending", "interrupted"]
                videos = [v for v in videos if v["status"] in statuses]
            if args.limit:
                videos = videos[:args.limit]
            result = run_batch(db, config, videos, args.force)
            print(json.dumps(result, ensure_ascii=False))
            return 1 if result["failed"] else 0
        if args.command == "status":
            print(json.dumps({"counts": db.rows("SELECT status,COUNT(*) AS count FROM videos GROUP BY status"), "jobs": db.rows("SELECT * FROM processing_jobs ORDER BY started_at DESC LIMIT 15"), "pending_reviews": db.db.execute("SELECT COUNT(*) FROM review_items WHERE status='pending'").fetchone()[0]}, ensure_ascii=False, indent=2))
        elif args.command == "search":
            print(json.dumps(db.search(args.query), ensure_ascii=False, indent=2))
        elif args.command == "export":
            if args.batch_size <= 0:
                raise ValueError("batch-size必须大于0")
            folder, manifest = export_corpus(db, args.batch_size)
            print(json.dumps({"directory": str(folder), "manifest": manifest}, ensure_ascii=False, indent=2))
        elif args.command == "metadata":
            video = db.video(args.id)
            with db.db:
                sid = db.series(args.series) if args.series is not None else video["series_id"]
                ep = args.episode if args.episode is not None else video["episode"]
                total = args.total if args.total is not None else video["total_episodes"]
                if ep is not None and (ep < 1 or total is not None and ep > total):
                    raise ValueError("集数必须为正数且不超过总集数")
                db.db.execute("UPDATE videos SET title=?,series_id=?,episode=?,total_episodes=?,manual_metadata=1,updated_at=? WHERE id=?", (args.title if args.title is not None else video["title"], sid, ep, total, now(), args.id))
            print(f"{args.id}元信息已更新；历史运行metadata保持原样，导出索引以当前数据库为准。")
        elif args.command == "review":
            found = db.rows("SELECT * FROM review_items WHERE id=?", (args.review_id,))
            if not found:
                raise ValueError("找不到核对记录ID")
            r = found[0]
            if not args.evidence.strip() or not args.author.strip() or not args.text.strip():
                raise ValueError("核对者、文本及依据不能为空")
            with db.db:
                evidence = {"description": args.evidence, "previous_status": r["status"], "new_status": args.status, "apply_transcript":args.apply_transcript, "original_data": json.loads(r["data_json"])}
                db.db.execute("INSERT INTO annotations(video_id,review_id,start,end,author,kind,text,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (r["video_id"], r["id"], r["start"], r["end"], args.author, "human_review", args.text, json.dumps(evidence, ensure_ascii=False), now()))
                db.db.execute("UPDATE review_items SET status=? WHERE id=?", (args.status, r["id"]))
            if db.video(r["video_id"])["current_run"]:
                publish_view(db, r["video_id"])
            print("核对结果已追加独立注释；原ASR/OCR和历史修订未覆盖，单条核对不标记整片人工完成。")
        elif args.command == "import-legacy":
            print(json.dumps(import_legacy(db, args.directory), ensure_ascii=False, indent=2))
        elif args.command == "refresh":
            videos = [db.video(args.id)] if args.id else db.rows("SELECT * FROM videos WHERE current_run IS NOT NULL")
            for v in videos:
                publish_view(db, v["id"])
            print(f"已刷新{len(videos)}部展示文件。")
        elif args.command == "realign":
            from .maintenance import realign_existing
            for vid in dict.fromkeys(args.ids):
                print(json.dumps(realign_existing(db, vid, config), ensure_ascii=False))
        db.db.close()
    return 0
