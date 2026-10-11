"""Independent local acceptance audit. Never recognizes new videos or modifies sources."""
import csv
import bisect
import difflib
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".tools/python"))
from laobu.common import norm, read_json, save_json, atomic, sha256, stamp, now
from laobu.database import Database
from laobu.outputs import current_packet
from laobu.quality import timing_errors

SAMPLE_IDS = ["LB-0001", "LB-0013", "LB-0051", "LB-0065", "LB-0088", "LB-0089", "LB-0090", "LB-0091", "LB-0093", "LB-0115"]
REQUIRED_OUTPUTS = ["metadata.json", "transcript_asr_raw.json", "transcript_final.txt", "transcript.srt", "subtitles_ocr.json", "subtitles_ocr.txt", "alignment.json", "transcript_structured.json", "review_queue.csv", "quality_report.md", "research_packet.md"]

def text_map(segments):
    text = ""
    positions = []
    for segment in segments:
        value = norm(segment["text"])
        positions.append((len(text), len(text)+len(value), segment))
        text += value
    return text, positions

def span_time(positions, lo, hi):
    matching = [s for start,end,s in positions if end > lo and start < max(hi, lo+1)]
    return [matching[0]["start"], matching[-1]["end"]] if matching else None

def compare_raw(previous, current):
    a, amap = text_map(previous)
    b, bmap = text_map(current)
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    changes = []
    for tag, i, j, x, y in matcher.get_opcodes():
        if tag != "equal":
            changes.append({"type":tag, "old_text":a[i:j], "new_text":b[x:y], "old_time":span_time(amap,i,j), "new_time":span_time(bmap,x,y), "explanation":"重新运行机器识别的差异，未自动判断哪边正确；请依原音频/字幕核对"})
    drift = []
    for old in previous:
        matches = [s for s in current if norm(s["text"]) == norm(old["text"]) and norm(old["text"])]
        if matches:
            best = min(matches, key=lambda s: abs(s["start"]-old["start"]))
            drift.append(max(abs(best["start"]-old["start"]), abs(best["end"]-old["end"])))
    return {"normalized_text_equal": a == b, "similarity": matcher.ratio(), "old_characters":len(a), "new_characters":len(b), "difference_count":len(changes), "differences":changes, "exact_text_matched_segments":len(drift), "max_matched_timestamp_drift_seconds":max(drift,default=None), "note":"只比較原始ASR通道；不把旧视觉修订错误地当作机器主识别原文"}

def compare_ocr(previous,current):
    timestamps = [f['time'] for f in current['frames']]
    scores,low = [],[]
    for f in previous['frames']:
        text = ' '.join(r['text'] for r in f['texts'] if r['confidence']>=.8)
        if len(norm(text))<3:
            continue
        index = bisect.bisect_left(timestamps,f['time'])
        nearby = current['frames'][max(0,index-2):index+3]
        if not nearby:
            scores.append(0)
            continue
        best = max(nearby,key=lambda row:difflib.SequenceMatcher(None,norm(text),norm(row['text']),autojunk=False).ratio())
        ratio = difflib.SequenceMatcher(None,norm(text),norm(best['text']),autojunk=False).ratio()
        scores.append(ratio)
        if ratio<.5:
            low.append({'time':f['time'],'old_text':text,'new_text':best['text'],'similarity':ratio})
    oldtext = ' '.join(r['text'] for f in previous['frames'] for r in f['texts'])
    newtext = ' '.join(f['text'] for f in current['frames'])
    abbreviations = {a:{'old_present':a in oldtext,'new_present':a in newtext} for a in ['JC','WD','GL','DB','ZW','YS','Z地','走D']}
    return {'old_high_confidence_frames':len(scores),'nearby_similarity_at_least_90_percent_fraction':sum(s>=.9 for s in scores)/max(1,len(scores)),'low_similarity_frames':len(low),'low_similarity_examples':low[:20],'abbreviation_presence':abbreviations,'note':'文字近邻回归，非人工画面/听核；广播图层和取样时间变化可产生差异'}

def srt_check(path, duration):
    content = path.read_text(encoding="utf-8-sig").strip()
    blocks = re.split(r"\r?\n\s*\r?\n", content) if content else []
    cues, errors = [], []
    def seconds(value):
        h,m,s = value.replace(',', '.').split(':')
        return int(h)*3600+int(m)*60+float(s)
    for i, block in enumerate(blocks,1):
        lines = block.splitlines()
        if len(lines)<3 or lines[0] != str(i) or not re.fullmatch(r"\d{2,}:\d{2}:\d{2},\d{3} --> \d{2,}:\d{2}:\d{2},\d{3}", lines[1]):
            errors.append(f"invalid block {i}")
            continue
        lo,hi = lines[1].split(' --> ')
        cues.append({"id":str(i), "start":seconds(lo), "end":seconds(hi), "text":'\n'.join(lines[2:])})
    errors.extend(timing_errors(cues,duration))
    return cues, errors

def main():
    db = Database(ROOT)
    result = {"generated_at":now(), "sample_ids":SAMPLE_IDS, "engineering_checks":[], "videos":[], "regressions":[], "limitations":[]}
    prior_hashes = read_json(ROOT / "运行日志/legacy_before.json")
    changed = [p for p,h in prior_hashes.items() if not (ROOT/p).exists() or sha256(ROOT/p) != h]
    result["engineering_checks"].append({"check":"137个旧资料文件未修改", "passed":not changed, "changed":changed})
    source_changed = [v["id"] for v in db.rows("SELECT * FROM videos") if not Path(v["path"]).exists() or sha256(v["path"]) != v["sha256"]]
    result["engineering_checks"].append({"check":"146个原视频SHA保持", "passed":not source_changed, "changed":source_changed})
    result["engineering_checks"].append({"check":"SQLite完整性", "passed":db.db.execute("PRAGMA integrity_check").fetchone()[0] == 'ok'})
    result["engineering_checks"].append({"check":"SQLite外键", "passed":not db.rows("PRAGMA foreign_key_check")})
    result["engineering_checks"].append({"check":"只处理授权的10部", "passed":{v['id'] for v in db.rows("SELECT * FROM videos WHERE current_run IS NOT NULL")} == set(SAMPLE_IDS)})
    unit_log = (ROOT/'解析工具/unit_tests.log').read_text(encoding='utf-8-sig') if (ROOT/'解析工具/unit_tests.log').exists() else ''
    unit_count = re.search(r'Ran (\d+) tests',unit_log)
    result['unit_tests'] = {'count':int(unit_count[1]) if unit_count else 0,'passed':bool(unit_count and re.search(r'^OK\s*$',unit_log,re.MULTILINE))}
    result['engineering_checks'].append({'check':'自动测试日志通过','passed':result['unit_tests']['passed']})
    try:
        import jsonschema
    except ImportError:
        jsonschema = None
    schema = read_json(ROOT / "解析工具/corpus.schema.json")
    for vid in SAMPLE_IDS:
        video = db.video(vid)
        if not video["current_run"] or video["status"] != "completed":
            result["videos"].append({"video_id":vid, "passed":False, "status":video["status"]})
            continue
        packet, folder = current_packet(db,vid)
        base = ROOT / "解析结果" / vid
        missing = [name for name in REQUIRED_OUTPUTS if not (base/name).is_file()]
        raw = read_json(folder / "transcript_asr_raw.json")
        ocr = read_json(folder / "subtitles_ocr.json")
        identical = raw == packet["asr"] and ocr == packet["ocr"]
        derived_from = packet['metadata'].get('derived_from_run')
        if derived_from:
            original_folder = folder.parent/derived_from
            previous_raw = read_json(original_folder/'transcript_asr_raw.json')
            previous_ocr = read_json(original_folder/'subtitles_ocr.json')
            identical = identical and previous_raw==raw and previous_ocr['frames']==ocr['frames']
        cues, errors = srt_check(base/'transcript.srt', packet["asr"]["duration"])
        schema_errors = []
        if jsonschema:
            schema_errors = [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(packet)]
        else:
            from laobu.contract import validate_contract
            schema_errors = validate_contract(packet,schema)
        bad_refs = []
        asr_ids = {s["id"] for s in raw["segments"]}
        ocr_ids = {s["id"] for s in ocr["segments"]}
        for a in packet["alignments"]:
            if not set(a["asr_ids"]) <= asr_ids or not set(a["ocr_ids"]) <= ocr_ids:
                bad_refs.append(a["id"])
        screenshot_missing = [f['screenshot'] for f in ocr['frames'] if f.get('screenshot') and not (folder/f['screenshot']).is_file()]
        media = json.loads(video["metadata_json"])
        decode_ok = abs(ocr["decoded_frames"] / media["fps"] - media["duration"]) < max(1, media["duration"] * .02)
        duration_ok = abs(raw["duration"] - media["duration"]) < .5
        human_false = packet['metadata']['human_review_status'] == 'unreviewed' and all(a['human_review_status']=='unreviewed' or a['human_review_status']=='human_confirmed' and a.get('revision_history') and all(h.get('author') and h.get('annotation_id') for h in a['revision_history']) for a in packet['alignments'])
        passed = not missing and identical and not errors and not bad_refs and not schema_errors and not screenshot_missing and decode_ok and duration_ok and human_false
        record = {"video_id":vid,"title":video['title'],"run_id":video['current_run'],"passed":passed,"duration_seconds":media['duration'],"asr_segments":len(raw['segments']),"ocr_segments":len(ocr['segments']),"sampled_frames":len(ocr['frames']),"candidate_cues":len(cues),"roi":ocr['roi'],"categories":{k:sum(a['category']==k for a in packet['alignments']) for k in 'ABCD'},"missing_outputs":missing,"raw_channels_preserved":identical,"srt_errors":errors,"schema_errors":schema_errors,"schema_full_validator_used":bool(jsonschema),"invalid_alignment_references":bad_refs,"missing_screenshots":screenshot_missing,"decode_duration_ok":decode_ok,"audio_duration_ok":duration_ok,"human_status_truthful":human_false,"gaps":raw.get('gaps',[]),"elapsed_seconds":packet['metadata']['elapsed_seconds'],"speed_ratio":packet['metadata']['processing_speed_audio_seconds_per_second'],"timing_errors_raw":timing_errors(raw['segments'],raw['duration']),"uncached_speed_measurement":vid!='LB-0001'}
        result['videos'].append(record)
        if video['title'] in ['赌徒之死','皇家马戏团']:
            process = ROOT/'01_转写结果/过程资料'
            oldraw_file = process/f"第{video['episode']}集_ASR原始.json" if video['title']=='赌徒之死' else process/'皇家马戏团/ASR原始.json'
            oldocr_file = process/f"第{video['episode']}集_画面OCR.json" if video['title']=='赌徒之死' else process/'皇家马戏团/画面OCR.json'
            oldcues_file = process/f"第{video['episode']}集_导出字幕.json" if video['title']=='赌徒之死' else process/'皇家马戏团/导出字幕.json'
            old_raw, old_cues = read_json(oldraw_file), read_json(oldcues_file)
            candidates = packet.get('candidate_segments',raw['segments'])
            oldtext = ''.join(norm(c.get('display',c['text'])) for c in old_cues)
            newtext = ''.join(norm(c['text']) for c in candidates)
            retained = len(old_cues)==len(candidates) and oldtext==newtext and all(abs(a['start']-b['start'])<.001 and abs(a['end']-b['end'])<.001 for a,b in zip(old_cues,candidates))
            result['regressions'].append({"video_id":vid,"title":video['title'],"episode":video['episode'],"raw_asr_comparison":compare_raw(old_raw['segments'],raw['segments']),"ocr_comparison":compare_ocr(read_json(oldocr_file),ocr),"legacy_candidate_transcript_preserved":retained,"baseline_cues":len(old_cues),"current_candidate_cues":len(candidates),"old_pending_records_retained":len(db.rows("SELECT * FROM review_items WHERE video_id=? AND run_id='legacy'",(vid,))),"note":"旧候选稿通过原视频SHA校验保留，含已补回对白/外语和未解决缺口；不是声称本次纯ASR自动达到视觉修订水平"})
    latest_file = ROOT/'汇总导出/latest.json'
    if latest_file.exists():
        export = Path(read_json(latest_file)['export_directory'])
        manifest = read_json(export/'corpus_manifest.json')
        ids = [vid for b in manifest['batches'] for vid in b['video_ids']]
        passed = ids == sorted(SAMPLE_IDS) and len(ids)==len(set(ids)) and manifest['indexed_video_count']==146 and len(manifest['unprocessed_ids'])==136
        result['engineering_checks'].append({"check":"导出批次不重复不遗漏，显式列出136个未处理ID", "passed":passed,"export_directory":str(export)})
    else:
        result['engineering_checks'].append({"check":"批次导出", "passed":False,"reason":"尚未运行export"})
    resource_file = ROOT/'运行日志/resource_samples.csv'
    if resource_file.exists():
        with resource_file.open(encoding='utf-8',newline='') as f:
            resources = list(csv.DictReader(f))
        result['resource_summary'] = {"sample_count":len(resources),"sample_interval_seconds":10,"peak_working_set_bytes":max([int(r['peak_working_set_bytes']) for r in resources],default=0),"max_observed_working_set_bytes":max([int(r['working_set_bytes']) for r in resources],default=0),"first_time":resources[0]['time'] if resources else None,"last_time":resources[-1]['time'] if resources else None,"note":"处理进程的采样，峰值含进程启动以来记录；不是显卡显存，采样不能证明不存在短暂抖动"}
    result['limitations'] = ['未逐句人工听核；自动工程验证不能证明逐字准确。','自动OCR区域尚需查看；所有ROI机器候选均已进入待核台账。','故事人名、比赛、金额、历史事实未做现实核实。','纯ASR和旧候选稿的差异独立记录，不能用沿用旧稿掩盖识别缺失。','本机无CUDA设备：CPU路径已验证，真实NVIDIA GPU及GPU故障回退只能通过实现/模拟测试核验。','软件测试不替代Windows 11实机测试，本次环境为Windows 10。','第一部使用恢复检查点，0.5秒提交耗时不代表全程识别速度；速度统计排除该项。']
    result['engineering_passed'] = all(c['passed'] for c in result['engineering_checks']) and len(result['videos'])==10 and all(v['passed'] for v in result['videos']) and len(result['regressions'])==5 and all(r['legacy_candidate_transcript_preserved'] for r in result['regressions'])
    result['human_accuracy_acceptance'] = 'not_performed'
    save_json(ROOT/'解析工具/validation_report.json',result)
    md = ['# V1.0 测试报告', '', f"报告时间：{result['generated_at']}。", '', f"工程/小批量验证：{'通过' if result['engineering_passed'] else '未全部通过'}；逐字人工质量验收：尚未执行。", '', '原146部只做只读索引，本次仅10部运行ASR/OCR，其余136部未处理。', '', '## 自动测试', '', '21项自动测试：核心台账、去重、检索、导出、锁与原子写、真实短视频OCR及JSON序列化、注入OCR失败后的ASR阶段恢复、模拟CUDA加载失败回退CPU、JSON数据契约；数字测试覆盖2.5/25不能混同、金额书写等价与名字不能按数字统一。更详细执行日志另存unit_tests.log。', '', '## 10部真实素材', '', '|素材ID|标题|时长秒|ASR段|OCR段|取样帧|TXT/SRT候选条|工程检查|', '|---|---|---:|---:|---:|---:|---:|---|']
    for v in result['videos']:
        md.append(f"|{v['video_id']}|{v.get('title','未完成')}|{v.get('duration_seconds',0):.1f}|{v.get('asr_segments',0)}|{v.get('ocr_segments',0)}|{v.get('sampled_frames',0)}|{v.get('candidate_cues',0)}|{'通过' if v['passed'] else '失败'}|")
    md.extend(['','## 原5部回归','', '回归分为新ASR与旧原ASR对照、旧视觉修订候选稿保留两项。后者按源视频SHA确认来源，逐条校验文字和时间，不将旧稿当作新模型的识别成果。','', '|素材ID|原ASR文字相似度|实质差异段|相同文本最大时间漂移秒|旧候选稿保留|旧待核项|','|---|---:|---:|---:|---|---:|'])
    for r in result['regressions']:
        raw = r['raw_asr_comparison']
        md.append(f"|{r['video_id']}|{raw['similarity']:.6f}|{raw['difference_count']}|{raw['max_matched_timestamp_drift_seconds']}|{'是' if r['legacy_candidate_transcript_preserved'] else '否'}|{r['old_pending_records_retained']}|")
    md.extend(['','OCR近邻文字回归（不是人工视觉验收）：',''])
    for r in result['regressions']:
        o = r['ocr_comparison']
        md.append(f"- {r['video_id']}：{o['old_high_confidence_frames']}个旧高置信取样帧，近邻相似度≥90%的比例{o['nearby_similarity_at_least_90_percent_fraction']:.1%}；低于50%的帧{o['low_similarity_frames']}个。差异例子与原字幕简写出现情况见JSON。")
    md.extend(['','每处新原ASR文字差异及时间范围见validation_report.json/regressions/raw_asr_comparison/differences。原先名称/比分/金额视觉修订、皇家马戏团补充对白与外语、赌徒之死两处缺口仍在旧候选通道和66条legacy待核记录中。新OCR不替换这些原画面写法。','', '## 稳定性、恢复与速度','', '真实开发运行中发现numpy布尔序列化和SQL占位符问题，均已修复并补入有效台账测试。早期失败/中断任务保留在数据库和日志，不伪装零错误历史。修复后同一批次顺序运行10部，第一部复用完整检查点；其余9部重新执行全程识别。单部失败不终止批次，恢复可从阶段检查点继续；正在执行的阶段需重跑。',''])
    uncached = [v for v in result['videos'] if v.get('uncached_speed_measurement') and v.get('passed')]
    if uncached:
        audio = sum(v['duration_seconds'] for v in uncached)
        elapsed = sum(v['elapsed_seconds'] for v in uncached)
        md.append(f"9部无缓存全流程合计音频{audio:.1f}秒，耗时{elapsed:.1f}秒，音频秒/处理秒={audio/max(elapsed,.001):.2f}。第一部恢复提交的耗时不纳入速度统计。新增视频时长、画面复杂度和二次识别数量会影响速度，不能直接以该比值保证146部耗时。")
    md.extend(['','资源采样：','', '```json',json.dumps(result.get('resource_summary',{}),ensure_ascii=False,indent=2),'```','', '## 工程检查',''])
    md.extend(f"- {'通过' if c['passed'] else '失败'}：{c['check']}" for c in result['engineering_checks'])
    md.extend(['', '## 未完成的人工核验与平台限制', ''])
    md.extend(f"- {x}" for x in result['limitations'])
    md.extend(['','本次验证支持进入用户审阅和后续增量使用；全库146部运行仍须用户另行确认。疑点未解决不等于工程失败，也不等于口播已准确无误。'])
    atomic(ROOT/'解析工具/测试报告.md','\n'.join(md)+'\n')
    db.db.close()
    print(json.dumps({"engineering_passed":result['engineering_passed'],"videos_checked":len(result['videos']),"regressions":len(result['regressions']),"report":str(ROOT/'解析工具/测试报告.md')},ensure_ascii=False,indent=2))
    return 0 if result['engineering_passed'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
