import json
import shutil
import time
import uuid
from .common import now, fingerprint, save_json
from .outputs import current_packet, write_packet, publish_view
from .quality import align
from .media import group_ocr

def realign_existing(db, vid, config):
    start = time.monotonic()
    video = db.video(vid)
    packet, previous = current_packet(db, vid)
    old_run = video["current_run"]
    run = "RUN-" + uuid.uuid4().hex
    workspace = db.output / "解析结果" / vid / ".work" / run
    shutil.copytree(previous, workspace)
    packet["ocr"]["segments"] = group_ocr(packet["ocr"]["frames"], packet["asr"]["duration"], packet["ocr"]["sampling_interval"])
    alignments, reviews = align(vid, packet["asr"], packet["ocr"], config)
    packet["alignments"], packet["reviews"] = alignments, reviews
    packet["metadata"]["run_id"] = run
    packet["metadata"]["derived_from_run"] = old_run
    packet["metadata"]["derived_at"] = now()
    packet["metadata"]["derived_operation"] = "regroup_and_realign_original_asr_and_ocr_frames_preserved"
    # Leave the original ASR/OCR processing parameters intact and record only the new QC settings.
    packet["metadata"]["alignment_parameters"] = {"abbreviations": config["abbreviations"]}
    write_packet(workspace, packet)
    final = db.output / "解析结果" / vid / "runs" / run
    workspace.rename(final)
    db.commit_run(video, run, packet)
    with db.db:
        db.db.execute("INSERT INTO processing_jobs(id,video_id,status,stage,config_hash,started_at,finished_at,elapsed_seconds) VALUES (?,?,'completed','realign',?,?,?,?)", (run, vid, fingerprint(config), now(), now(), time.monotonic()-start))
    publish_view(db, vid)
    return {"video_id": vid, "new_run": run, "derived_from": old_run, "categories": {k:sum(a["category"]==k for a in alignments) for k in "ABCD"}, "note": "历史台账未删除，旧机器疑点仍保留原状态；没有把重建对齐当成人工完成"}
