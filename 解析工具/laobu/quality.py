import difflib
import re
from .common import fingerprint, norm

RESEARCH_FIELDS = ["故事人物", "人物关系", "涉及的体育赛事", "年份和时间线", "金额和财务变化", "人物关键行为", "赌博心理变化", "家庭及朋友关系变化", "重要生活细节", "关键对话", "前后呼应的情节", "疑似悬念与伏笔", "叙事结构标签", "结尾方式"]

def contained(needle, haystack):
    for match in re.finditer(re.escape(needle),haystack):
        before = haystack[match.start()-1:match.start()] if match.start() else ''
        after = haystack[match.end():match.end()+1]
        if needle[0].isdigit() and before and (before.isdigit() or before in '.:+-'):
            continue
        if needle[-1].isdigit() and after and (after.isdigit() or after in '.:%'):
            continue
        return True
    return False

def review(vid, start, end, kind, text, evidence):
    payload = {"video_id": vid, "start": round(start, 3), "end": round(end, 3), "kind": kind, "text": text, "evidence": evidence}
    payload["id"] = "R-" + fingerprint(payload)
    payload["status"] = "pending"
    return payload

def classify(asr, ocr, config):
    a, b = norm(asr), norm(ocr)
    if not a or not b:
        return "D", ["missing_channel"], 0.
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    if a == b:
        return "A", ["normalized_equal"], ratio
    if len(a) >= 3 and contained(a,b):
        return "A", ["asr_matches_part_of_longer_subtitle"], ratio
    expanded = b
    used = []
    unknown = []
    for abbreviation, readings in config["abbreviations"].items():
        token = norm(abbreviation)
        if token in b:
            reading = next((x for x in readings if norm(x) in a), None)
            if reading:
                expanded = expanded.replace(token, norm(reading))
                used.append(abbreviation)
            else:
                unknown.append(abbreviation)
    if expanded == a and used:
        return "B", ["intentional_abbreviation"], ratio
    if len(a) >= 3 and contained(a,expanded) and used:
        return "B", ["intentional_abbreviation", "subtitle_spans_neighboring_asr"], ratio
    # Normal writing differences have to be literal equivalents, not inferred facts.
    if normalize_numbers(a) == normalize_numbers(expanded) and not unknown:
        return "B", ["numeric_writing_difference"] + (["intentional_abbreviation"] if used else []), ratio
    if len(a) >= 3 and contained(normalize_numbers(a),normalize_numbers(expanded)) and not unknown:
        return "B", ["numeric_writing_difference", "subtitle_spans_neighboring_asr"], ratio
    types = ["text_conflict"]
    if unknown:
        types.append("abbreviation_reading_unknown")
    elif used:
        types.append("abbreviation_plus_other_conflict")
    if re.findall(r"\d+(?:[.:]\d+)?", normalize_numbers(a)) != re.findall(r"\d+(?:[.:]\d+)?", normalize_numbers(expanded)):
        types.append("number_date_score_amount_candidate_conflict")
    if ratio < .25:
        return "D", types + ["unreliable_correspondence"], ratio
    return "C", types, ratio

def normalize_numbers(text):
    # Conservative integer normalization, only runs containing a numeral, never "万一".
    digits = dict(zip("零〇一二三四五六七八九两", [0,0,1,2,3,4,5,6,7,8,9,2]))
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}
    def convert(match):
        s = match[0]
        if s[0] in "万亿":
            return s
        following = text[match.end():match.end()+1]
        preceding = text[max(0,match.start()-1):match.start()]
        if len(s)==1 and s in digits and following not in set("年月日号元块岁个人场次秒分钟点比成倍万亿") and preceding not in {"点","比"}:
            return s
        if all(c in digits for c in s):
            return ''.join(str(digits[c]) for c in s)
        total = section = number = 0
        for c in s:
            if c in digits:
                number = digits[c]
            else:
                u = units[c]
                if u < 10000:
                    section += (number or 1) * u
                else:
                    section += number
                    total = (total + section) * u if u == 100000000 else total + section * u
                    section = 0
                number = 0
        return str(total + section + number)
    result = re.sub(r"[零〇一二三四五六七八九两十百千万亿]+", convert, text)
    result = re.sub(r"(?<=\d)点(?=\d)", ".", result)
    result = re.sub(r"(?<=\d)比(?=\d)", ":", result)
    def scaled(match):
        number = float(match[1]) * {"万":10000,"亿":100000000}[match[2]]
        return str(int(number)) if number.is_integer() else str(number)
    return re.sub(r"(\d+(?:\.\d+)?)(万|亿)", scaled, result)

def align(vid, asr, ocr, config):
    alignments, reviews = [], []
    ocr_segments = ocr["segments"]
    for s in asr["segments"]:
        matches = [o for o in ocr_segments if min(o["end"], s["end"]) - max(o["start"], s["start"]) > min(.12, (s["end"]-s["start"])/3)]
        texts = []
        for o in matches:
            if not texts or norm(o["text"]) != norm(texts[-1]):
                texts.append(o["text"])
        text = " ".join(texts)
        category, types, ratio = classify(s["text"], text, config)
        confidence = {"avg_logprob": s["avg_logprob"], "no_speech_prob": s["no_speech_prob"], "compression_ratio": s.get("compression_ratio"), "ocr_min": min([o["confidence"] for o in matches], default=None), "text_similarity": ratio}
        if s["avg_logprob"] < -1 or s["no_speech_prob"] > .6:
            types.append("low_asr_confidence")
            category = "D"
        if s.get("compression_ratio", 0) > 2.4 or s["no_speech_prob"] > .6:
            types.append("possible_music_effect_hallucination")
            category = "D"
        if matches and min(o["confidence"] for o in matches) < .75:
            types.append("low_ocr_confidence")
            category = "D"
        a = {"id": "AL-" + s["id"], "video_id": vid, "start": s["start"], "end": s["end"], "asr_ids": [s["id"]], "ocr_ids": [o["id"] for o in matches], "asr_raw": s["text"], "ocr_raw": text, "candidate_transcript": s["text"], "revision_basis": "原始ASR；没有自动用OCR覆盖或润色", "revision_history": [], "category": category, "difference_types": types, "confidence": confidence, "human_review_status": "unreviewed", "speaker_type": "unknown"}
        alignments.append(a)
        if category in ["C", "D"]:
            reviews.append(review(vid, s["start"], s["end"], "+".join(types), s["text"], {"asr": s["text"], "ocr": text, "asr_id": s["id"], "ocr_ids": a["ocr_ids"], "category": category}))
    for o in ocr_segments:
        nearby = [s for s in asr['segments'] if min(o['end']+.25,s['end'])>max(o['start']-.25,s['start'])]
        if nearby:
            spoken = ''.join(s['text'] for s in nearby)
            screen = norm(o['text'])
            for abbreviation,readings in config['abbreviations'].items():
                reading = next((r for r in readings if norm(r) in norm(spoken)),None)
                if reading:
                    screen = screen.replace(norm(abbreviation),norm(reading))
            before,after = normalize_numbers(norm(spoken)),normalize_numbers(screen)
            diff = difflib.SequenceMatcher(None,before,after,autojunk=False)
            unmatched = [after[j:k] for tag,_,__,j,k in diff.get_opcodes() if tag in ('insert','replace') and k-j>=3]
            if unmatched:
                evidence = {'ocr_id':o['id'],'ocr_raw':o['text'],'local_asr_ids':[s['id'] for s in nearby],'local_asr_raw':spoken,'unmatched_screen_text':unmatched,'basis':'时间附近ASR无法匹配的画面文字；可能为漏词、OCR错误、标题或时间错位，不猜补'}
                reviews.append(review(vid,o['start'],o['end'],'possible_asr_omission_or_non_speech_ocr',o['text'],evidence))
                alignments.append({'id':'AL-UNMATCHED-'+o['id'],'video_id':vid,'start':o['start'],'end':o['end'],'asr_ids':[s['id'] for s in nearby],'ocr_ids':[o['id']],'asr_raw':spoken,'ocr_raw':o['text'],'candidate_transcript':'','revision_basis':evidence['basis'],'revision_history':[],'category':'D','difference_types':['possible_asr_omission_or_non_speech_ocr'],'confidence':{'ocr_min':o['confidence']},'human_review_status':'unreviewed','speaker_type':'unknown'})
        if not any(min(o["end"], s["end"]) > max(o["start"], s["start"]) for s in asr["segments"]):
            a = {"id": "AL-" + o["id"], "video_id": vid, "start": o["start"], "end": o["end"], "asr_ids": [], "ocr_ids": [o["id"]], "asr_raw": "", "ocr_raw": o["text"], "candidate_transcript": "", "revision_basis": "无口播识别；不能从字幕猜补口播", "revision_history": [], "category": "D", "difference_types": ["ocr_without_asr"], "confidence": {"ocr_min": o["confidence"]}, "human_review_status": "unreviewed", "speaker_type": "unknown"}
            alignments.append(a)
            reviews.append(review(vid, o["start"], o["end"], "ocr_without_asr", o["text"], {"ocr_id": o["id"]}))
    for g in asr.get("gaps", []):
        reviews.append(review(vid, g["start"], g["end"], g["kind"], g["text"], {"source": "silero_vad"}))
    for retry in asr.get("retries", []):
        if retry["language"] != config["language"]:
            target = retry["target"]
            reviews.append(review(vid, target["start"], target["end"], "possible_foreign_or_dialogue", "二次识别检测到其他语言；说话人/影视来源未知", retry))
    if ocr["roi_detection"]["status"] in ["pending", "machine_candidate"]:
        reviews.append(review(vid, 0, asr["duration"], "roi_unverified", "字幕区域由机器推定，请检查预览，确认没有遗漏/混入标题或水印", {"roi": ocr["roi"], "preview": "roi_preview.jpg"}))
    for f in ocr["frames"]:
        if f.get("screenshot") and f["texts"]:
            reviews.append(review(vid, f["time"], min(asr["duration"], f["time"] + config["sample_interval"]), "ocr_unreliable_frame", f["text"], {"screenshot": f["screenshot"]}))
    alignments.sort(key=lambda a: (a["start"], a["id"]))
    return alignments, reviews

def extract_candidates(vid, segments, terms):
    fields = {k: [] for k in RESEARCH_FIELDS}
    candidates = []
    for s in segments:
        matches = [("explicit_term", term) for term in terms if term in s["text"]]
        patterns = [("年份和时间线", r"(?:19|20)\d{2}年(?:\d{1,2}月(?:\d{1,2}[日号])?)?|\d{1,2}月\d{1,2}[日号]"), ("金额和财务变化", r"[\d零一二三四五六七八九十百千万亿两]+(?:万|亿)?(?:多|余)?(?:元|块钱|块|人民币)"), ("score_candidate", r"\d+\s*[:：比]\s*\d+")]
        for kind, pattern in patterns:
            matches.extend((kind, m[0]) for m in re.finditer(pattern, s["text"]))
        for kind, value in matches:
            c = {"video_id": vid, "start": s["start"], "end": s["end"], "segment_id": s["id"], "kind": kind, "value": value, "evidence_text": s["text"], "source": "literal_asr_text", "status": "machine_candidate", "fact_verified": False}
            candidates.append(c)
            if kind in fields:
                fields[kind].append(c)
    return {"fields": fields, "literal_candidates": candidates, "semantic_analysis": [], "note": "仅明确文字匹配；不自动判断人名、关系或剧情，不将故事事件视为核实事实"}

def timing_errors(segments, duration):
    errors = []
    for i, s in enumerate(segments):
        if not (0 <= s["start"] < s["end"] <= duration + .1):
            errors.append({"id": s["id"], "kind": "out_of_bounds_or_empty"})
        if i and s["start"] < segments[i - 1]["end"] - .001:
            errors.append({"id": s["id"], "kind": "overlap"})
    return errors
