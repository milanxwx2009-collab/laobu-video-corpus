import ctypes
import importlib.metadata
import math
import os
import platform
import subprocess
import sys
import time
import wave
from pathlib import Path
from .common import now, save_json, sha256

def runtime_info():
    packages = {}
    for name in ["faster-whisper", "ctranslate2", "av", "rapidocr-onnxruntime", "onnxruntime", "numpy", "imageio-ffmpeg"]:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = "missing"
    memory = None
    if os.name == "nt":
        class Memory(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [(x, ctypes.c_ulonglong) for x in ["total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended"]]
        m = Memory()
        m.length = ctypes.sizeof(m)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            memory = {"total_bytes": m.total, "available_bytes": m.available}
    try:
        import ctranslate2
        cuda = ctranslate2.get_cuda_device_count()
    except Exception as e:
        cuda = str(e)
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    version = subprocess.run([exe, "-version"], capture_output=True, text=True, check=True).stdout.splitlines()[0]
    return {"time": now(), "python": sys.version, "executable": sys.executable, "platform": platform.platform(), "cpu_logical": os.cpu_count(), "memory": memory, "cuda_devices": cuda, "packages": packages, "ffmpeg": exe, "ffmpeg_version": version}

def probe(path):
    import av
    with av.open(str(path)) as container:
        video = next((s for s in container.streams if s.type == "video"), None)
        if video is None:
            raise ValueError("无视频流")
        duration = float(container.duration / av.time_base) if container.duration else float(video.duration * video.time_base) if video.duration else 0
        if duration <= 0:
            raise ValueError("无法获取有效时长")
        return {"duration": duration, "width": video.width, "height": video.height, "fps": float(video.average_rate) if video.average_rate else None, "video_codec": video.codec_context.name, "size_bytes": path.stat().st_size, "audio_streams": [{"codec": s.codec_context.name, "sample_rate": s.codec_context.sample_rate, "channels": s.codec_context.channels} for s in container.streams if s.type == "audio"]}

def extract_audio(source, dest):
    import imageio_ffmpeg
    temp = dest.with_suffix(".tmp.wav")
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-v", "error", "-xerror", "-i", str(source), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", str(temp)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"FFmpeg音频提取失败 ({result.returncode}): {result.stderr}")
    with wave.open(str(temp)) as f:
        if f.getnframes() == 0:
            raise RuntimeError("音轨为空")
    os.replace(temp, dest)
    return {"command": cmd, "exit_code": result.returncode, "stderr": result.stderr, "audio_sha256": sha256(dest)}

def audio_samples(path):
    import numpy as np
    with wave.open(str(path)) as f:
        if f.getframerate() != 16000 or f.getsampwidth() != 2 or f.getnchannels() != 1:
            raise ValueError("音频必须为16kHz单声道PCM16")
        return np.frombuffer(f.readframes(f.getnframes()), np.int16).astype(np.float32) / 32768

def uncovered_intervals(speech, segments, minimum=.35):
    intervals = [(max(0, s["start"] - .1), s["end"] + .1) for s in segments]
    gaps = []
    for c in speech:
        spans = [(c["start"] / 16000, c["end"] / 16000)]
        for start, end in intervals:
            remainder = []
            for lo, hi in spans:
                if end <= lo or start >= hi:
                    remainder.append((lo, hi))
                else:
                    if lo < start:
                        remainder.append((lo, start))
                    if end < hi:
                        remainder.append((end, hi))
            spans = remainder
        gaps.extend({"start": round(lo, 3), "end": round(hi, 3), "kind": "vad_uncovered", "status": "pending", "text": "机器VAD检测到未覆盖语音，可能为对白、外语、音乐或误检"} for lo, hi in spans if hi - lo >= minimum)
    return gaps

class Engines:
    def __init__(self, config, log):
        self.config = config
        self.log = log
        self.model = None
        self.ocr = None
        self.device = None
        self.fallback = None

    def load_model(self, cpu=False):
        from faster_whisper import WhisperModel
        import ctranslate2
        requested = self.config["device"]
        device = "cpu" if cpu else ("cuda" if requested == "auto" and ctranslate2.get_cuda_device_count() else "cpu" if requested == "auto" else requested)
        try:
            self.model = WhisperModel(self.config["model"], device=device, compute_type="int8" if device == "cpu" else "float16", cpu_threads=self.config["cpu_threads"], num_workers=1, download_root=self.config["model_dir"], local_files_only=True)
        except Exception as e:
            if device != "cuda":
                raise RuntimeError(f"本地ASR模型加载失败，不会联网下载：{e}") from e
            self.fallback = str(e)
            self.log(f"GPU加载失败，回退CPU int8：{e}")
            return self.load_model(cpu=True)
        self.device = device
        self.log(f"模型已加载：{device}，CPU线程={self.config['cpu_threads']}")

    def load_ocr(self):
        if self.ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            self.ocr = RapidOCR(intra_op_num_threads=self.config["ocr_threads"], inter_op_num_threads=1, det_limit_side_len=128, det_limit_type="min", use_cls=False)
        return self.ocr

    def recognize(self, audio, language="zh", vad=True):
        if self.model is None:
            self.load_model()
        kwargs = dict(language=language, beam_size=self.config["beam_size"], word_timestamps=True, vad_filter=vad, condition_on_previous_text=False)
        try:
            iterator, info = self.model.transcribe(audio, **kwargs)
            return self.collect(iterator), info
        except Exception as e:
            if self.device != "cuda":
                raise
            self.fallback = str(e)
            self.log(f"GPU推理失败，整段重新用CPU处理：{e}")
            self.load_model(cpu=True)
            iterator, info = self.model.transcribe(audio, **kwargs)
            return self.collect(iterator), info

    def collect(self, iterator):
        rows = []
        last_log = time.monotonic()
        for s in iterator:
            rows.append({"id": f"ASR-{s.id:06}", "source_id": s.id, "start": s.start, "end": s.end, "text": s.text, "avg_logprob": s.avg_logprob, "no_speech_prob": s.no_speech_prob, "compression_ratio": s.compression_ratio, "speaker_type": "unknown", "language": self.config["language"], "human_review_status": "unreviewed", "words": [{"start": w.start, "end": w.end, "word": w.word, "probability": w.probability} for w in (s.words or [])]})
            if time.monotonic() - last_log > 20:
                self.log(f"ASR已识别至 {s.end:.1f}秒，{len(rows)}段")
                last_log = time.monotonic()
        return rows

    def transcribe(self, audio, progress_path):
        from faster_whisper.vad import get_speech_timestamps, VadOptions
        samples = audio_samples(audio)
        rows, info = self.recognize(samples, self.config["language"], self.config["vad_filter"])
        # Persist full primary ASR before auditing/retries, so a later crash cannot erase it.
        primary = {"segments": rows, "language": info.language, "duration": len(samples) / 16000}
        save_json(progress_path, primary)
        speech = get_speech_timestamps(samples, VadOptions(threshold=.5, min_silence_duration_ms=100, speech_pad_ms=0))
        gaps = uncovered_intervals(speech, rows)
        retries = []
        targets = [{"start": s["start"], "end": s["end"], "kind": "low_confidence"} for s in rows if s["avg_logprob"] < -1 or s["no_speech_prob"] > .6 or s["compression_ratio"] > 2.4]
        targets.extend(gaps)
        if self.config["retry_low_confidence"]:
            for index, target in enumerate(targets[:self.config["retry_limit"]], 1):
                lo, hi = max(0, target["start"] - .3), min(len(samples) / 16000, target["end"] + .3)
                if hi - lo > self.config["max_gap_retry_seconds"]:
                    continue
                self.log(f"二次识别候选 {index}/{min(len(targets),self.config['retry_limit'])}：{lo:.2f}—{hi:.2f}s，{target['kind']}")
                # Language detection for gap clips; these remain candidates and never replace narration.
                clip_rows, clip_info = self.recognize(samples[int(lo * 16000):int(hi * 16000)], language=None, vad=False)
                for s in clip_rows:
                    s["start"] += lo
                    s["end"] += lo
                    s["language"] = clip_info.language
                    for w in s["words"]:
                        w["start"] += lo
                        w["end"] += lo
                retries.append({"target": target, "language": clip_info.language, "segments": clip_rows, "applied": False, "basis": "关闭VAD的二次机器识别，仅供核对"})
        primary.update(model=self.config["model"], device=self.device, compute_type="int8" if self.device == "cpu" else "float16", fallback_reason=self.fallback, speech_intervals=[{"start": c["start"] / 16000, "end": c["end"] / 16000} for c in speech], gaps=gaps, retries=retries, retry_targets_total=len(targets), retry_targets_processed=len(retries), quality_note="机器覆盖检测不证明无漏字；未知说话人不强行判断，二次识别不会自动补写原口播")
        return primary

    def detect_roi(self, source, duration):
        import cv2
        import numpy as np
        cap = cv2.VideoCapture(str(source))
        votes = []
        evidence = []
        try:
            for t in [duration * f for f in [.04, .17, .33, .52, .71, .88]]:
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
                ok, frame = cap.read()
                if not ok:
                    continue
                h, w = frame.shape[:2]
                small = cv2.resize(frame, (540, max(1, round(h * 540 / w))))
                result, _ = self.load_ocr()(small)
                for box, text, confidence in result or []:
                    y = float(np.mean([p[1] for p in box]) / small.shape[0])
                    if .42 <= y <= .92 and len(text) >= 3 and confidence >= .65:
                        votes.append((y, min(len(text), 18) * confidence))
                        evidence.append({"time": t, "text": text, "y_fraction": y, "confidence": float(confidence)})
        finally:
            cap.release()
        if not votes:
            return [0, .42, 1, .92], {"method": "fallback", "status": "pending", "evidence": evidence}
        centers = [i / 100 for i in range(45, 90)]
        best = max(centers, key=lambda y: sum(weight for py, weight in votes if abs(py - y) < .035))
        return [0, max(.0, best - .06), 1, min(1., best + .06)], {"method": "multi_frame_text_band_vote", "status": "machine_candidate", "evidence": evidence, "note": "候选字幕区域可能包含标题/水印；请查看roi_preview.jpg，必要时人工指定"}

    def extract_ocr(self, source, dest, duration):
        import cv2
        import numpy as np
        ocr = self.load_ocr()
        roi = self.config.get("roi")
        detection = {"method": "manual", "status": "user_configured"}
        if roi is None:
            roi, detection = self.detect_roi(source, duration)
        cap = cv2.VideoCapture(str(source))
        if not cap.isOpened():
            raise RuntimeError("OpenCV无法打开视频")
        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps <= 0:
            raise RuntimeError("无法读取取帧帧率")
        interval = self.config["sample_interval"]
        frames, raw_index, next_time = [], 0, 0.
        previous, previous_result, previous_text = None, None, None
        burst = 0
        last_log = time.monotonic()
        dest.mkdir(parents=True, exist_ok=True)
        screenshot_dir = dest / "screenshots"
        screenshot_dir.mkdir(exist_ok=True)
        try:
            while cap.grab():
                t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
                if t < 0 or (t == 0 and raw_index):
                    t = raw_index / fps
                raw_index += 1
                if t + .001 < next_time:
                    continue
                ok, image = cap.retrieve()
                if not ok:
                    raise RuntimeError(f"取帧失败：{t:.3f}s")
                h, w = image.shape[:2]
                x1, y1, x2, y2 = roi
                crop = image[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)]
                if not crop.size:
                    raise ValueError("字幕区域为空")
                crop = cv2.resize(crop, (1080, max(1, round(crop.shape[0] * 1080 / crop.shape[1]))))
                if not frames:
                    preview = image.copy()
                    cv2.rectangle(preview, (int(x1*w), int(y1*h)), (int(x2*w), int(y2*h)), (0,0,255), 3)
                    write_image(dest / "roi_preview.jpg", preview)
                signature = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), (160, 24))
                reused = bool(previous is not None and np.max(np.abs(signature.astype(np.int16) - previous.astype(np.int16))) <= 1)
                if reused:
                    texts = previous_result
                else:
                    result, _ = ocr(crop)
                    texts = [{"text": r[1], "confidence": float(r[2]), "box_crop": [[float(v) for v in p] for p in r[0]], "content_type": "subtitle_candidate"} for r in (result or [])]
                    texts.sort(key=lambda r: (round(min(p[1] for p in r["box_crop"]) / 20), min(p[0] for p in r["box_crop"])))
                text = " ".join(r["text"] for r in texts)
                screenshot = None
                if not texts or any(r["confidence"] < .75 for r in texts):
                    screenshot = f"screenshots/{len(frames):06}.jpg"
                    write_image(dest / screenshot, crop)
                frames.append({"time": round(t, 4), "texts": texts, "text": text, "screenshot": screenshot, "reused_identical_frame": reused})
                if self.config["adaptive_sampling"] and text != previous_text:
                    burst = 2
                step = interval / 2 if burst else interval
                burst = max(0, burst - 1)
                next_time = t + step
                previous, previous_result, previous_text = signature, texts, text
                if time.monotonic() - last_log > 20:
                    self.log(f"OCR已取样至 {t:.1f}/{duration:.1f}秒，{len(frames)}帧")
                    last_log = time.monotonic()
        finally:
            cap.release()
        if raw_index == 0 or raw_index / fps < duration - max(1., duration * .02):
            raise RuntimeError(f"视频解码提前结束：解码{raw_index / fps:.1f}s，容器时长{duration:.1f}s")
        return {"roi": roi, "roi_detection": detection, "sampling_interval": interval, "adaptive_sampling": self.config["adaptive_sampling"], "decoded_frames": raw_index, "sampling_uncertainty_seconds": interval, "frames": frames, "segments": group_ocr(frames, duration, interval), "note": "取样不是逐帧文字核验，极短字幕可能遗漏；文字保持原样，类型为候选字幕，不能确保排除了所有水印"}

def write_image(path, image):
    import cv2
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise RuntimeError("截图编码失败")
    # imwrite on older OpenCV builds fails on non-ASCII Windows paths.
    Path(path).write_bytes(encoded.tobytes())

def group_ocr(frames, duration, interval):
    from .common import norm
    rows = []
    current = None
    previous_time = 0.
    for i, f in enumerate(frames):
        text = f["text"]
        boundary = max(0., (previous_time + f["time"]) / 2) if i else 0.
        if current and (norm(text) != norm(current["text"]) or f["time"] - previous_time > interval * 1.6):
            current["end"] = boundary
            rows.append(current)
            current = None
        if text:
            if current is None:
                current = {"id": f"OCR-{len(rows)+1:06}", "start": boundary, "end": f["time"], "text": text, "confidence": 1., "sample_indices": [], "content_type": "subtitle_candidate", "timing_uncertainty_seconds": interval, "human_review_status": "unreviewed"}
            current["confidence"] = min(current["confidence"], *[r["confidence"] for r in f["texts"]])
            current["sample_indices"].append(i)
        previous_time = f["time"]
    if current:
        current["end"] = min(duration, previous_time + interval / 2)
        rows.append(current)
    return rows
