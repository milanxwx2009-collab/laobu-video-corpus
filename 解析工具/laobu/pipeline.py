import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from . import VERSION, SCHEMA_VERSION
from .common import now, fingerprint, read_json, save_json, sha256
from .media import Engines, extract_audio, runtime_info
from .outputs import write_packet, publish_view
from .quality import align, extract_candidates, timing_errors

def make_logger(output):
    output = Path(output)
    (output / "运行日志").mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("laobu")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    file = logging.FileHandler(output / "运行日志" / (time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6] + ".log"), encoding="utf-8")
    file.setFormatter(formatter)
    logger.addHandler(stream)
    logger.addHandler(file)
    return logger.info

def process_video(db, video, engines, config, log, force=False):
    start = time.monotonic()
    vid = video["id"]
    source = Path(video["path"])
    if not source.is_file():
        # Find a still-valid duplicate path; never assume moved sources exist.
        aliases = db.rows("SELECT path FROM source_paths WHERE video_id=? ORDER BY last_seen DESC", (vid,))
        source = next((Path(a["path"]) for a in aliases if Path(a["path"]).is_file()), source)
    run = "RUN-" + uuid.uuid4().hex
    runtime = runtime_info()
    model_root = Path(config["model_dir"])
    model_files = sorted(model_root.glob("**/config.json"))
    runtime["model_cache_configs"] = [{"path": str(p), "sha256": sha256(p)} for p in model_files]
    cfg_hash = fingerprint({"config": config, "version": VERSION, "packages": runtime["packages"], "model_configs": runtime["model_cache_configs"]})
    workspace = db.output / "解析结果" / vid / ".work" / (run if force else cfg_hash)
    workspace.mkdir(parents=True, exist_ok=True)
    if not force and not (workspace / "asr.json").exists():
        # Files can be committed by rename before the DB transaction; retain the
        # unindexed run and copy it to a new staging directory for safe recovery.
        for prior in db.rows("SELECT id FROM processing_jobs WHERE video_id=? AND config_hash=? AND status IN ('failed','interrupted') ORDER BY started_at DESC", (vid, cfg_hash)):
            orphan = db.output / "解析结果" / vid / "runs" / prior["id"]
            if (orphan / "asr.json").exists() and (orphan / "ocr.json").exists():
                shutil.copytree(orphan, workspace, dirs_exist_ok=True)
                log(f"{vid}恢复文件已提交但数据库未提交的检查点：{prior['id']}")
                break
    with db.db:
        db.db.execute("INSERT INTO processing_jobs(id,video_id,status,stage,config_hash,started_at,runtime_json) VALUES (?,?,'running','validate',?,?,?)", (run, vid, cfg_hash, now(), json.dumps(runtime, ensure_ascii=False)))
        db.db.execute("UPDATE videos SET status='running',last_error=NULL WHERE id=?", (vid,))
    def stage(name):
        with db.db:
            db.db.execute("UPDATE processing_jobs SET stage=? WHERE id=?", (name, run))
        log(f"{vid} [{name}] {video['title']}")
    try:
        if not source.is_file():
            raise FileNotFoundError(f"找不到原文件：{source}；请扫描其新目录")
        if sha256(source) != video["sha256"]:
            raise RuntimeError("原路径文件内容已改变，请重新扫描；不会用旧素材ID处理不同内容")
        audio = workspace / "audio.wav"
        stage("audio")
        if not (audio.exists() and (workspace / "audio_extraction.json").exists()):
            save_json(workspace / "audio_extraction.json", extract_audio(source, audio))
        stage("asr")
        if (workspace / "asr.json").exists():
            asr = read_json(workspace / "asr.json")
            log(f"{vid}复用已完整提交的ASR阶段")
        else:
            asr = engines.transcribe(audio, workspace / "asr_primary_checkpoint.json")
            save_json(workspace / "asr.json", asr)
        stage("ocr")
        if (workspace / "ocr.json").exists():
            ocr = read_json(workspace / "ocr.json")
            log(f"{vid}复用已完整提交的OCR阶段")
        else:
            ocr = engines.extract_ocr(source, workspace, json.loads(video["metadata_json"])["duration"])
            save_json(workspace / "ocr.json", ocr)
        stage("align")
        alignments, reviews = align(vid, asr, ocr, config)
        errors = timing_errors(asr["segments"], asr["duration"])
        for error in errors:
            log(f"{vid}时间异常（保留原时间并进入质量报告）：{error}")
        elapsed = time.monotonic() - start
        meta = {"schema_version": SCHEMA_VERSION, "tool_version": VERSION, "video_id": vid, "run_id": run, "title": video["title"], "original_filename": video["original_filename"], "source_path": str(source), "source_paths": db.rows("SELECT path FROM source_paths WHERE video_id=?", (vid,)), "sha256": video["sha256"], "series_id": video["series_id"], "episode": video["episode"], "total_episodes": video["total_episodes"], "media": json.loads(video["metadata_json"]), "processed_at": now(), "parameters": config, "runtime": runtime, "elapsed_seconds": elapsed, "processing_speed_audio_seconds_per_second": asr["duration"] / max(elapsed, .001), "quality_status": "machine_processed_unreviewed", "human_review_status": "unreviewed", "fact_verified": False}
        packet = {"schema_version": SCHEMA_VERSION, "metadata": meta, "asr": asr, "ocr": ocr, "alignments": alignments, "reviews": reviews, "research": extract_candidates(vid, asr["segments"], config["evidence_terms"])}
        stage("export")
        write_packet(workspace, packet)
        # Check the source remains the indexed byte sequence; no output ever targets it.
        if sha256(source) != video["sha256"]:
            raise RuntimeError("处理期间原文件发生变化，本次结果不提交")
        final = db.output / "解析结果" / vid / "runs" / run
        final.parent.mkdir(exist_ok=True)
        workspace.rename(final)
        db.commit_run(video, run, packet)
        with db.db:
            db.db.execute("UPDATE processing_jobs SET status='completed',stage='done',finished_at=?,elapsed_seconds=? WHERE id=?", (now(), time.monotonic() - start, run))
        publish_view(db, vid)
        log(f"{vid}完成：耗时{time.monotonic()-start:.1f}s，音频/耗时比{meta['processing_speed_audio_seconds_per_second']:.2f}；未人工听核")
        return True
    except BaseException as e:
        status = "interrupted" if isinstance(e, KeyboardInterrupt) else "failed"
        message = f"{type(e).__name__}: {e}"
        # If DB commit already succeeded, keep the completed run and retry publication later.
        committed = db.video(vid)["current_run"] == run
        with db.db:
            if not committed:
                db.db.execute("UPDATE videos SET status=?,last_error=?,updated_at=? WHERE id=?", (status, message, now(), vid))
                db.db.execute("UPDATE processing_jobs SET status=?,finished_at=?,elapsed_seconds=?,error=? WHERE id=?", (status, now(), time.monotonic()-start, message, run))
        log(f"{vid} {'结果已提交但视图发布失败，可用refresh修复' if committed else status}：{message}")
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        return False

def run_batch(db, config, selected, force=False):
    log = make_logger(db.output)
    engines = Engines(config, log)
    success = failure = 0
    log(f"开始小批量：{len(selected)}部；单任务顺序运行；原视频只读")
    for i, video in enumerate(selected):
        log(f"进度：本批完成={success}，失败={failure}，运行中=1，待处理={len(selected)-i-1}")
        if process_video(db, video, engines, config, log, force):
            success += 1
        else:
            failure += 1
    log(f"批次结束：完成={success}，失败={failure}，运行中=0，待处理=0")
    return {"completed": success, "failed": failure}
