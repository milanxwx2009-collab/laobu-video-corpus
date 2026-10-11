import json
import shutil
from pathlib import Path
from . import VERSION, SCHEMA_VERSION
from .common import atomic, now, save_csv, save_json, stamp, fingerprint
from .quality import timing_errors

REVIEW_FIELDS = ["id", "video_id", "run_id", "start", "end", "kind", "status", "text", "data_json", "created_at"]

def write_packet(folder, packet, reviews=None, annotations=None):
    folder = Path(folder)
    meta, asr, ocr = packet["metadata"], packet["asr"], packet["ocr"]
    reviews = packet["reviews"] if reviews is None else reviews
    annotations = annotations or []
    cues = packet.get("candidate_segments") or asr["segments"]
    save_json(folder / "metadata.json", meta)
    save_json(folder / "transcript_asr_raw.json", asr)
    save_json(folder / "subtitles_ocr.json", ocr)
    save_json(folder / "alignment.json", packet["alignments"])
    save_json(folder / "transcript_structured.json", packet)
    save_json(folder / "annotations.json", annotations)
    save_json(folder / "legacy_review_records.json", packet.get("legacy_review_records", []))
    csv_reviews = [{**r, "run_id": r.get("run_id", meta["run_id"]), "created_at": r.get("created_at", meta.get("processed_at", now())), "data_json": r.get("data_json", json.dumps(r, ensure_ascii=False))} for r in reviews]
    save_csv(folder / "review_queue.csv", csv_reviews, REVIEW_FIELDS)
    lines = [f"{meta['video_id']} {meta['title']}", "机器转写工作稿：未逐句人工听核；final为文件名约定，不代表定稿。", ""]
    srt = []
    if packet.get("candidate_segments"):
        lines.insert(2, f"候选来源：{packet.get('candidate_basis','verified_legacy_work_draft')}。新ASR/OCR和修订依据独立保留；整片仍未逐句人工听核。")
    for i, s in enumerate(cues, 1):
        lines.append(f"[{stamp(s['start'])}—{stamp(s['end'])}] {s['text']}")
        srt.append(f"{i}\n{stamp(s['start'], True)} --> {stamp(s['end'], True)}\n{s['text']}\n")
    lines.extend(["", "未覆盖语音区间（没有猜补文字）："] + [f"[{stamp(g['start'])}—{stamp(g['end'])}] 【待人工核对】{g['text']}" for g in asr.get("gaps", [])])
    atomic(folder / "transcript_final.txt", "\n".join(lines) + "\n")
    atomic(folder / "transcript.srt", "\n".join(srt))
    atomic(folder / "subtitles_ocr.txt", "\n".join(f"[{stamp(s['start'])}—{stamp(s['end'])}] {s['text']}" for s in ocr["segments"]) + "\n")
    errors = timing_errors(cues, asr["duration"])
    categories = {k: sum(a["category"] == k for a in packet["alignments"]) for k in "ABCD"}
    report = [f"# {meta['video_id']} 质量说明", "", "状态：自动处理已完成，人工逐句听核未完成，故事事实未核实。", "", f"ASR {len(asr['segments'])}段；OCR {len(ocr['segments'])}段 / {len(ocr['frames'])}取样；A/B/C/D={categories}。", f"未覆盖VAD区间：{len(asr.get('gaps', []))}；待核台账：{sum(r.get('status','pending') == 'pending' for r in reviews)}条（含历史未解决记录）。", f"二次识别目标：{asr.get('retry_targets_total', 0)}，实际候选：{asr.get('retry_targets_processed', 0)}；没有自动应用。", f"字幕时间格式问题：{json.dumps(errors, ensure_ascii=False)}", f"OCR区域：{ocr['roi']}；定位方法：{ocr['roi_detection']['method']}；采样时间误差约±{ocr['sampling_interval']}秒。", "", "说话人类型均为unknown。VAD、语言检测、置信度与重复文本只能提示疑点，无法可靠自动区分旁白、影视对白、音效和音乐。", "自动对照A也不等于人工完成；B是文字简写/书写等价候选；C/D保持待核。名称与数字冲突只能作为候选风险，分句错位也可能产生误报。", "原始ASR/OCR从未互相覆盖。二次ASR见transcript_asr_raw.json/retries。未识别和低置信截图见screenshots。", "自动定位ROI和非字幕文字类型仍需人工确认。取样遗漏的瞬间字幕无法被本工具证明不存在。", "人工修订以annotations独立附加，历史台账和每次运行均保留。"]
    atomic(folder / "quality_report.md", "\n\n".join(report) + "\n")
    md = [f"# {meta['video_id']} · {meta['title']}", "", "## 来源与质量", "", "机器资料，不是事实核验报告。未逐句人工听核，不做剧情推断。", "", "```json", json.dumps(meta, ensure_ascii=False, indent=2), "```", "", "## 完整口播", ""]
    if packet.get("candidate_segments"):
        md.append(f"候选来源：{packet.get('candidate_basis','verified_legacy_work_draft')}。旧Work候选按视频SHA匹配，人工修订仅依明确resolved记录；整片未逐句听核，新机器原文另列。")
    md.extend(f"- [{stamp(s['start'])}—{stamp(s['end'])}] ({s['id']}) {s['text']}" for s in cues)
    if packet.get("candidate_segments"):
        md.extend(["", "## 本次新ASR原文（独立通道）", ""])
        md.extend(f"- [{stamp(s['start'])}—{stamp(s['end'])}] ({s['id']}) {s['text']}" for s in asr["segments"])
    md.extend(["", "## 原画面字幕", ""])
    md.extend(f"- [{stamp(s['start'])}—{stamp(s['end'])}] ({s['id']}) {s['text']}" for s in ocr["segments"])
    md.extend(["", "## 双通道差异", "", "A=规范化一致，B=可解释简写，C=冲突，D=不能可靠确认。所有等级均为机器判断。", ""])
    for a in packet["alignments"]:
        if a["category"] != "A":
            md.append(f"- [{stamp(a['start'])}—{stamp(a['end'])}] {a['category']} / {','.join(a['difference_types'])}\n  - ASR原文：{a['asr_raw']}\n  - OCR原文：{a['ocr_raw']}\n  - 候选口播：{a['candidate_transcript']}\n  - 依据：{a['revision_basis']}")
    md.extend(["", "## 明确文字候选（未核实）", ""])
    md.extend(f"- [{stamp(c['start'])}—{stamp(c['end'])}] {c['kind']}：{c['value']}；依据：{c['evidence_text']} ({c['segment_id']})" for c in packet["research"]["literal_candidates"])
    md.extend(["", "其他研究字段预留为空，不推断人物关系、心理变化、伏笔或文学评价。", "", "## 核对台账与缺口", ""])
    md.extend(f"- [{stamp(r['start'])}—{stamp(r['end'])}] {r['id']} / {r.get('status','pending')} / {r['kind']}：{r.get('text','')}" for r in reviews)
    md.extend(["", "## 独立人工/历史注释", "", "```json", json.dumps(annotations, ensure_ascii=False, indent=2), "```", "", "## 处理方法与限制", "", *report[2:]])
    if packet.get("legacy_review_records"):
        md.extend(["", "## 旧核对依据与修订（不是本次人工完成）", "", "```json", json.dumps(packet["legacy_review_records"], ensure_ascii=False, indent=2), "```"])
    atomic(folder / "research_packet.md", "\n".join(md) + "\n")

def current_packet(db, vid):
    v = db.video(vid)
    if not v["current_run"]:
        raise ValueError(f"{vid}尚无完整结果")
    folder = db.output / "解析结果" / vid / "runs" / v["current_run"]
    from .common import read_json
    packet = read_json(folder / "transcript_structured.json")
    # Derived runs may contain a rendered human overlay. Reconstruct it from
    # persistent annotations each time, so later pending/ignored status is honored.
    packet.pop('candidate_segments',None)
    packet.pop('candidate_basis',None)
    for a in packet['alignments']:
        if a.get('human_review_status')=='human_confirmed':
            a['candidate_transcript']=a['asr_raw']
            a['human_review_status']='unreviewed'
            a['revision_basis']='原始ASR；人工修订从持久annotations重新投影'
            a['revision_history']=[]
    packet["metadata"]["current_index"] = {k: v[k] for k in ["title", "series_id", "episode", "total_episodes", "path"]}
    records = db.rows("SELECT * FROM legacy_records WHERE video_id=? ORDER BY source,row_number", (vid,))
    notes, cues = [], []
    for record in records:
        data = json.loads(record["data_json"])
        if data.get("record_type") == "legacy_cue":
            if data["video_sha256"] != v["sha256"]:
                continue
            cue = data["cue"]
            cues.append({"id": f"LEGACY-CUE-{record['row_number']:06}", "start": cue["start"], "end": cue["end"], "text": cue.get("display", cue["text"]), "legacy_spoken_text": cue["text"], "annotation_source": record["source"], "basis": data["scope"], "human_review_status": "unreviewed", "channel":"legacy_candidate", "original_record": cue})
        else:
            notes.append({"source": record["source"], "row": record["row_number"], "record": data})
    if cues:
        packet["candidate_segments"] = sorted(cues, key=lambda s: (s["start"], s["end"]))
        packet["candidate_basis"] = "verified_legacy_work_draft"
        for a in packet["alignments"]:
            relevant = [c for c in cues if min(c["end"], a["end"]) > max(c["start"], a["start"])]
            if relevant:
                a["legacy_candidate_transcript"] = " ".join(c["text"] for c in relevant)
                a["legacy_candidate_ids"] = [c["id"] for c in relevant]
                a["legacy_revision_basis"] = "旧Work稿，原视频SHA相同，记录独立保留；不代表本次人工听核"
    packet["legacy_review_records"] = notes
    annotations = db.rows("SELECT * FROM annotations WHERE video_id=? ORDER BY id", (vid,))
    from .reviews import apply_human_candidates
    packet = apply_human_candidates(packet,annotations,{r['id']:r['status'] for r in db.reviews(vid)})
    return packet, folder

def publish_view(db, vid):
    packet, run_folder = current_packet(db, vid)
    root = db.output / "解析结果" / vid
    reviews = db.reviews(vid)
    annotations = db.rows("SELECT * FROM annotations WHERE video_id=? ORDER BY id", (vid,))
    write_packet(root, packet, reviews, annotations)
    # Images/audio remain in their immutable run directory. A source pointer avoids copies.
    save_json(root / "current.json", {"run_id": packet["metadata"]["run_id"], "artifact_directory": str(run_folder), "screenshots": str(run_folder / "screenshots"), "audio": str(run_folder / "audio.wav")})

def export_corpus(db, batch_size):
    import uuid
    videos = db.rows("SELECT * FROM videos ORDER BY id")
    ready = [v for v in videos if v["current_run"]]
    export_id = now().replace(":", "").replace("+", "_") + "_" + uuid.uuid4().hex[:8]
    base = db.output / "汇总导出"
    staging = base / (".tmp_" + export_id)
    staging.mkdir(parents=True)
    save_csv(staging / "all_videos.csv", videos, list(videos[0]) if videos else ["id", "title", "status"])
    reviews = db.rows("SELECT * FROM review_items ORDER BY video_id,start,id")
    save_csv(staging / "all_review_items.csv", reviews, REVIEW_FIELDS)
    save_json(staging / "series.json", {"series": db.rows("SELECT * FROM series ORDER BY id"), "relations": [{"video_id": v["id"], "series_id": v["series_id"], "episode": v["episode"], "total_episodes": v["total_episodes"]} for v in videos]})
    segments, candidate_segments, subtitles, annotations, legacy_records, batches = [], [], [], [], [], []
    quality = []
    for index in range(0, len(ready), batch_size):
        group = ready[index:index + batch_size]
        batch = staging / f"batch_{index // batch_size + 1:03}"
        batch.mkdir()
        texts, ids = [], []
        for v in group:
            packet, folder = current_packet(db, v["id"])
            own_reviews = db.reviews(v["id"])
            own_notes = db.rows("SELECT * FROM annotations WHERE video_id=? ORDER BY id", (v["id"],))
            write_packet(batch / v["id"], packet, own_reviews, own_notes)
            notes = {"run_id": v["current_run"], "video_id": v["id"], "title": v["title"], "sha256": v["sha256"], "source_path": v["path"], "artifact_directory": str(folder)}
            save_json(batch / v["id"] / "source_pointer.json", notes)
            segments.extend({**s, **notes} for s in packet["asr"]["segments"])
            candidate_segments.extend({**s, **notes, "channel": s.get("channel","legacy_candidate" if packet.get("candidate_segments") else "asr_candidate")} for s in packet.get("candidate_segments", packet["asr"]["segments"]))
            subtitles.extend({**s, **notes} for s in packet["ocr"]["segments"])
            annotations.extend(own_notes)
            legacy_records.extend({"video_id": v["id"], **r} for r in packet.get("legacy_review_records", []))
            quality.append({"video_id": v["id"], "run_id": v["current_run"], "status": v["status"], "human_review_status": "unreviewed", "pending_reviews": sum(r["status"] == "pending" for r in own_reviews), "gaps": packet["asr"].get("gaps", []), "timing_errors": timing_errors(packet["asr"]["segments"], packet["asr"]["duration"]), "elapsed_seconds": packet["metadata"].get("elapsed_seconds")})
            texts.append((batch / v["id"] / "research_packet.md").read_text(encoding="utf-8"))
            ids.append(v["id"])
        atomic(batch / "research_batch.md", "\n\n---\n\n".join(texts))
        batches.append({"batch": batch.name, "video_ids": ids})
    for filename, rows in [("all_segments.jsonl", segments), ("all_candidate_segments.jsonl", candidate_segments), ("all_subtitles.jsonl", subtitles)]:
        atomic(staging / filename, ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))
    save_json(staging / "annotations.json", annotations)
    save_json(staging / "legacy_review_records.json", legacy_records)
    save_json(staging / "quality_summary.json", quality)
    manifest = {"schema_version": SCHEMA_VERSION, "tool_version": VERSION, "export_id": export_id, "exported_at": now(), "indexed_video_count": len(videos), "exported_video_count": len(ready), "unprocessed_ids": [v["id"] for v in videos if not v["current_run"]], "batches": batches, "source_policy": "原视频未复制、未上传；音频和截图通过source_pointer定位本地版本目录", "human_review_status": "unreviewed"}
    assert [x for b in batches for x in b["video_ids"]] == [v["id"] for v in ready]
    save_json(staging / "corpus_manifest.json", manifest)
    final = base / export_id
    staging.rename(final)
    # Immutable export committed before updating the convenience index.
    for filename in ["all_videos.csv", "all_review_items.csv", "all_segments.jsonl", "all_candidate_segments.jsonl", "all_subtitles.jsonl", "corpus_manifest.json", "quality_summary.json", "series.json", "annotations.json", "legacy_review_records.json"]:
        atomic(base / filename, (final / filename).read_text(encoding="utf-8"))
    save_json(base / "latest.json", {"export_directory": str(final), "export_id": export_id})
    return final, manifest
