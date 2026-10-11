"""Double-click workflow and a separate, clearly labelled delivery folder."""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import run  # Bootstrap the existing offline runtime and local dependencies.
from laobu.cli import load_config
from laobu.common import CorpusLock, atomic, now, save_csv, save_json
from laobu.database import Database
from laobu.outputs import current_packet, export_corpus, write_packet

CONFIG = Path(__file__).with_name("config.json")
EXPECTED = 146


def collect_results(config):
    output = Path(config["output_dir"])
    destination = output / "全部146部视频结果"
    with CorpusLock(output / "数据库"):
        db = Database(output)
        try:
            videos = db.rows("SELECT * FROM videos ORDER BY id")
            destination.mkdir(parents=True, exist_ok=True)
            rows = []
            for video in videos:
                folder = destination / "逐部结果" / video["id"]
                folder.mkdir(parents=True, exist_ok=True)
                available = bool(video["current_run"])
                if available:
                    packet, source = current_packet(db, video["id"])
                    write_packet(folder, packet, db.reviews(video["id"]),
                                 db.rows("SELECT * FROM annotations WHERE video_id=? ORDER BY id", (video["id"],)))
                    if (source / "screenshots").is_dir():
                        shutil.copytree(source / "screenshots", folder / "screenshots", dirs_exist_ok=True)
                    if (source / "roi_preview.jpg").is_file():
                        shutil.copy2(source / "roi_preview.jpg", folder / "roi_preview.jpg")
                    save_json(folder / "source_pointer.json", {
                        "video_id": video["id"], "run_id": video["current_run"],
                        "original_video": video["path"], "artifact_directory": str(source),
                        "audio": str(source / "audio.wav"),
                        "note": "截图已复制；原视频、提取音频和历史版本在原项目中保留。",
                    })
                    pending_note = folder / "尚未完成.txt"
                    if pending_note.exists():
                        pending_note.unlink()
                else:
                    atomic(folder / "尚未完成.txt", f"{video['id']} · {video['title']}\n尚未生成完整结果。\n状态：{video['status']}\n再次双击一键启动文件可继续处理。\n")
                label = {"completed": "已完成", "pending": "待处理", "failed": "失败待重试",
                         "interrupted": "中断待续跑", "running": "处理中"}.get(video["status"], video["status"])
                rows.append({"视频编号": video["id"], "标题": video["title"], "集数": video["episode"],
                             "处理状态": label, "已有结果": "是" if available else "否",
                             "结果子文件夹": f"逐部结果/{video['id']}", "错误说明": video["last_error"] or ""})
            export_folder, export_manifest = export_corpus(db, 10)
            summary = destination / "汇总资料"
            summary.mkdir(exist_ok=True)
            for source in export_folder.iterdir():
                if source.is_file():
                    shutil.copy2(source, summary / source.name)
            batches = summary / "批次研究包"
            batches.mkdir(exist_ok=True)
            for batch in export_manifest["batches"]:
                shutil.copy2(export_folder / batch["batch"] / "research_batch.md", batches / f"{batch['batch']}.md")
            completed = sum(v["status"] == "completed" and bool(v["current_run"]) for v in videos)
            missing = [v["id"] for v in videos if v["status"] != "completed" or not v["current_run"]]
            manifest = {"updated_at": now(), "expected_video_count": EXPECTED,
                        "indexed_video_count": len(videos), "completed_video_count": completed,
                        "remaining_video_ids": missing, "all_146_completed": len(videos) == EXPECTED and not missing,
                        "result_directory": str(destination), "export_directory": str(export_folder),
                        "human_review_status": "unreviewed"}
            save_csv(destination / "全部视频目录.csv", rows,
                     ["视频编号", "标题", "集数", "处理状态", "已有结果", "结果子文件夹", "错误说明"])
            save_json(destination / "完成情况.json", manifest)
            atomic(destination / "先看这里.txt", (
                f"老布视频结果\n更新时间：{manifest['updated_at']}\n"
                f"已索引 {len(videos)} 部；已完成 {completed} 部；尚未完成 {len(missing)} 部。\n\n"
                "1. 打开「全部视频目录.csv」查看标题、编号、完成状态。\n"
                "2. 在「逐部结果」中打开对应编号的文件夹。\n"
                "   transcript_final.txt：带时间的口播文字。\n"
                "   transcript.srt：字幕文件。\n"
                "   subtitles_ocr.txt：原画面字幕文字。\n"
                "   research_packet.md：该视频研究资料。\n"
                "   review_queue.csv：待核对问题。\n"
                "   其余JSON文件：原始识别与结构化数据；screenshots：核对截图。\n"
                "3.「汇总资料」保存全库数据；「批次研究包」每10部合成一个研究文件。\n\n"
                "未完成的视频只有说明文件，不能视为已有结果。\n"
                "再次双击解析工具里的「一键跑完全部视频.bat」可续跑并更新此文件夹。\n"
                "这些结果是机器处理稿，尚未逐句人工核对。\n"
            ))
            return manifest
        finally:
            db.db.close()


def command(*args):
    return subprocess.run([sys.executable, "-X", "utf8", "-u", str(Path(__file__).with_name("run.py")),
                           "--config", str(CONFIG), *args], cwd=CONFIG.parent).returncode


def main():
    parser = argparse.ArgumentParser(description="一键处理全部146部视频，并集中保存结果；已完成的会跳过。")
    parser.add_argument("--collect-only", action="store_true", help="只整理已有结果，不启动视频识别")
    args = parser.parse_args()
    config = load_config(CONFIG)
    if args.collect_only:
        result = collect_results(config)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0
    print("即将处理全部146部视频。已完成的跳过，中断和失败的继续处理。\n请保持窗口打开，并让电脑保持唤醒。", flush=True)
    print("\n第1步：检查视频目录……", flush=True)
    if command("scan"):
        print("扫描遇到错误，尚未开始识别。请查看上方提示。", flush=True)
        return 1
    initial = collect_results(config)
    if initial["indexed_video_count"] != EXPECTED:
        print(f"当前索引不是146部，而是{initial['indexed_video_count']}部。请检查视频目录，暂未启动全量识别。", flush=True)
        return 1
    print(f"\n第2步：已完成{initial['completed_video_count']}部，继续处理{len(initial['remaining_video_ids'])}部……", flush=True)
    interrupted = False
    try:
        process_exit = command("process", "--resume", "--all-confirmed")
    except KeyboardInterrupt:
        interrupted = True
        process_exit = 130
    print("\n第3步：整理已生成的结果和汇总资料……", flush=True)
    result = collect_results(config)
    print(f"\n结果文件夹：{result['result_directory']}", flush=True)
    if result["all_146_completed"]:
        print("全部146部视频处理完成，结果已保存。", flush=True)
        return 0
    print(f"当前完成{result['completed_video_count']}/146部。再次双击同一个启动文件即可续跑。", flush=True)
    return 130 if interrupted else process_exit or 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断。再次双击启动文件即可续跑。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"\n运行未完成：{type(exc).__name__}: {exc}\n再次双击可续跑，已提交的识别结果会保留。", file=sys.stderr)
        raise SystemExit(1)
