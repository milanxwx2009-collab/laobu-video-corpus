import json
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL))
sys.path.insert(0, str(TOOL.parent / ".tools/python"))
from laobu.common import atomic, read_json, parse_title, sha256, CorpusLock
from laobu.database import Database
from laobu.media import group_ocr, uncovered_intervals
from laobu.quality import classify, extract_candidates, timing_errors, align
from laobu.outputs import export_corpus
from laobu.pipeline import process_video

class CorpusTest(unittest.TestCase):
    def setUp(self):
        scratch = TOOL / "tests/.tmp"
        scratch.mkdir(exist_ok=True)
        # mkdtemp's private Windows ACL does not work with restricted tokens.
        self.root = scratch / ("case_" + uuid.uuid4().hex)
        self.root.mkdir()
        self.db = Database(self.root)
        self.config = read_json(TOOL / "config.json")
    def tearDown(self):
        self.db.db.close()
        assert self.root.resolve().is_relative_to((TOOL / "tests/.tmp").resolve())
        shutil.rmtree(self.root)
    def scan_fake(self, name="中文 空格《赌徒之死》第1_4集.mp4", content=b"immutable-source"):
        path = self.root / "素材" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
        result = self.db.scan(path.parent, lambda p: {"duration": 2}, path)
        return result[0]["id"], path
    def packet(self, vid, run="run1"):
        asr = {"duration": 2, "segments": [{"id": "ASR-000001", "start": 0., "end": 1., "text": "王二欠网贷两万元", "avg_logprob": -.2, "no_speech_prob": .01, "words": []}], "gaps": []}
        ocr = {"segments": [{"id": "OCR-000001", "start": 0., "end": 1., "text": "王二欠WD两万元", "confidence": .99}], "frames": [], "roi": [0,.6,1,.72], "roi_detection": {"method": "manual"}, "sampling_interval": .4}
        ocr["roi_detection"]["status"] = "user_configured"
        comparisons, reviews = align(vid, asr, ocr, self.config)
        return {"schema_version": 1, "metadata": {"video_id": vid, "title": "测试", "run_id": run}, "asr": asr, "ocr": ocr, "alignments": comparisons, "reviews": reviews, "research": {"literal_candidates": []}}
    def commit(self, vid, run):
        packet = self.packet(vid, run)
        folder = self.root / "解析结果" / vid / "runs" / run
        folder.mkdir(parents=True)
        atomic(folder / "transcript_structured.json", json.dumps(packet, ensure_ascii=False))
        self.db.commit_run(self.db.video(vid), run, packet)
    def test_hash_id_survives_duplicate_rename_and_rescan_order(self):
        vid, path = self.scan_fake()
        renamed = path.with_name("移动改名.mp4")
        path.rename(renamed)
        result = self.db.scan(renamed.parent, lambda _: self.fail("duplicate must not probe"))
        self.assertEqual(vid, result[0]["id"])
        self.assertTrue(result[0]["duplicate"])
        self.assertEqual(1, len(self.db.rows("SELECT * FROM videos")))
    def test_same_path_new_bytes_gets_new_id(self):
        vid, path = self.scan_fake()
        path.write_bytes(b"other content")
        self.assertNotEqual(vid, self.db.scan(path.parent, lambda _: {"duration": 2})[0]["id"])
    def test_series_and_episodes(self):
        self.assertEqual(("赌徒之死", "赌徒之死", 1, 4), parse_title("《赌徒之死》1_4集.mp4"))
        self.assertEqual(("皇家马戏团", None, None, None), parse_title("《皇家马戏团》.mp4"))
    def test_bad_file_does_not_abort_scan(self):
        good = self.root / "a.mp4"
        bad = self.root / "b.mp4"
        good.write_bytes(b"good")
        bad.write_bytes(b"bad")
        def probe(path):
            if path == bad:
                raise ValueError("corrupt")
            return {"duration": 1}
        result = self.db.scan(self.root, probe)
        self.assertEqual(1, sum(r.get("error") is not None for r in result))
        self.assertEqual(1, len(self.db.rows("SELECT * FROM videos")))
    def test_abbreviations_do_not_overwrite_raw(self):
        self.assertEqual("B", classify("他欠了网贷", "他欠了WD", self.config)[0])
        self.assertIn(classify("他欠了网贷五万元", "他欠了WD六万元", self.config)[0], ["C", "D"])
        self.assertEqual("A", classify("他走了。", "他走了", self.config)[0])
        self.assertEqual("B", classify("两万元", "20000元", self.config)[0])
        self.assertEqual("B", classify("两万元", "2万元", self.config)[0])
        self.assertEqual("B", classify("比分二比二", "比分2:2", self.config)[0])
        self.assertIn(classify("2.5万元", "25万元", self.config)[0],["C","D"])
        self.assertIn(classify("王二", "王2", self.config)[0],["C","D"])
        self.assertIn(classify("万一", "10001", self.config)[0],["C","D"])
        for a,b in [('6万元','16万元'),('本金5000','本金50000'),('-2.5','2.5'),('收益30%','收益30'),('2:2','2:20')]:
            self.assertIn(classify(a,b,self.config)[0],['C','D'])
        self.assertEqual("D", classify("你好", "", self.config)[0])
        self.assertEqual("A", classify("某天早上", "某天早上，刘明用爷爷的信息", self.config)[0])
        self.assertEqual("B", classify("刘明欠了网贷", "某天刘明欠了WD十万元", self.config)[0])
    def test_ocr_duplicate_and_blank_time(self):
        def frame(t, text):
            return {"time": t, "text": text, "texts": [{"confidence": .99}] if text else []}
        rows = group_ocr([frame(0,"WD"), frame(.4,"WD"), frame(.8,""), frame(1.2,"WD"), frame(1.6,"网贷")], 2., .4)
        self.assertEqual(["WD", "WD", "网贷"], [r["text"] for r in rows])
        self.assertLess(rows[0]["end"], rows[1]["start"])
    def test_gap_audit_preserves_internal_holes(self):
        gaps = uncovered_intervals([{"start": 0, "end": 80000}], [{"start": 0, "end": 1}, {"start": 3, "end": 5}])
        self.assertEqual(1, len(gaps))
        self.assertAlmostEqual(1.1, gaps[0]["start"])
        self.assertAlmostEqual(2.9, gaps[0]["end"])
    def test_reprocessing_preserves_reviews_and_annotations(self):
        vid, _ = self.scan_fake()
        packet = self.packet(vid)
        packet["reviews"] = [{"id": "r1", "start": 0., "end": 1., "kind": "conflict", "text": "疑点"}]
        self.db.commit_run(self.db.video(vid), "first", packet)
        with self.db.db:
            self.db.db.execute("UPDATE review_items SET status='resolved' WHERE id='r1'")
            self.db.db.execute("INSERT INTO annotations(video_id,review_id,kind,text,evidence_json,created_at) VALUES (?,?,'human_review','确认','{}','now')", (vid, "r1"))
        self.db.commit_run(self.db.video(vid), "second", packet)
        self.assertEqual("resolved", self.db.reviews(vid)[0]["status"])
        self.assertEqual(1, len(self.db.rows("SELECT * FROM annotations")))
        self.assertEqual(2, len(self.db.rows("SELECT * FROM segments")))
    def test_fulltext_short_chinese_and_punctuation(self):
        vid, _ = self.scan_fake()
        self.commit(vid, "one")
        self.assertEqual(2, len(self.db.search("王二")))
        self.assertEqual(2, len(self.db.search("王二欠")))
        self.assertEqual([], self.db.search('" OR *'))
        self.assertEqual([], self.db.search("%"))
    def test_export_batches_no_omission_or_duplication(self):
        ids = []
        for i in range(5):
            vid, _ = self.scan_fake(f"video{i}.mp4", f"v{i}".encode())
            ids.append(vid)
            self.commit(vid, f"run{i}")
        directory, manifest = export_corpus(self.db, 2)
        self.assertEqual(ids, [v for b in manifest["batches"] for v in b["video_ids"]])
        self.assertEqual([2,2,1], [len(b["video_ids"]) for b in manifest["batches"]])
        self.assertTrue((directory / "all_subtitles.jsonl").exists())
        self.assertFalse(any(p.name.startswith(".tmp_") for p in directory.parent.iterdir()))
    def test_source_change_fails_without_touching_source(self):
        vid, path = self.scan_fake()
        path.write_bytes(b"new bytes")
        original = sha256(path)
        from laobu.media import Engines
        config = {**self.config, "model_dir": str(self.root)}
        self.assertFalse(process_video(self.db, self.db.video(vid), Engines(config, lambda _: None), config, lambda _: None))
        self.assertEqual(original, sha256(path))
        self.assertEqual("failed", self.db.video(vid)["status"])
    def test_crash_recovery_retains_finished_run(self):
        vid, _ = self.scan_fake()
        self.commit(vid, "finished")
        with self.db.db:
            self.db.db.execute("UPDATE videos SET status='running' WHERE id=?", (vid,))
        self.db.recover()
        self.assertEqual("interrupted", self.db.video(vid)["status"])
        self.assertEqual("finished", self.db.video(vid)["current_run"])
    def test_lock_rejects_concurrent_writer(self):
        with CorpusLock(self.root / "锁"):
            with self.assertRaises(RuntimeError):
                with CorpusLock(self.root / "锁"):
                    pass
    def test_atomic_chinese_paths(self):
        p = self.root / "中文 有空格" / "a.json"
        atomic(p, '{"中文":1}')
        atomic(p, '{"中文":2}')
        self.assertEqual({"中文":2}, read_json(p))
        self.assertEqual(["a.json"], [f.name for f in p.parent.iterdir()])
    def test_research_candidates_have_evidence_and_no_inference(self):
        c = extract_candidates("LB-1", [{"id": "s1", "start": 1, "end": 2, "text": "王二在2016年欠了两万元"}], ["王二"])
        self.assertEqual([], c["fields"]["人物关系"])
        self.assertTrue(all(r["evidence_text"] and r["video_id"] == "LB-1" for r in c["literal_candidates"]))
    def test_timestamp_checks(self):
        errors = timing_errors([{"id":"1", "start":0,"end":1},{"id":"2","start":.9,"end":3}], 2)
        self.assertEqual(2, len(errors))
    def test_failed_ocr_resumes_asr_checkpoint(self):
        import wave
        vid, path = self.scan_fake()
        packet = self.packet(vid)
        class FakeEngine:
            calls = 0
            failures = 1
            def transcribe(self, *_):
                self.calls += 1
                return packet['asr']
            def extract_ocr(self, *_):
                if self.failures:
                    self.failures -= 1
                    raise RuntimeError('injected OCR failure')
                return packet['ocr']
        engine = FakeEngine()
        def audio(_, dest):
            with wave.open(str(dest),'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
                w.writeframes(b'\0' * 64000)
            return {'test_audio':True}
        config = {**self.config, 'model_dir':str(self.root)}
        with patch('laobu.pipeline.extract_audio', side_effect=audio):
            self.assertFalse(process_video(self.db,self.db.video(vid),engine,config,lambda _:None))
            self.assertTrue(process_video(self.db,self.db.video(vid),engine,config,lambda _:None))
        self.assertEqual(1,engine.calls)
        self.assertEqual('completed',self.db.video(vid)['status'])
        self.assertEqual(2,len(self.db.rows('SELECT * FROM processing_jobs')))
        self.assertEqual(b'immutable-source',path.read_bytes())
    def test_real_ocr_frames_are_json_serializable(self):
        import cv2
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont
        from laobu.media import Engines
        source = self.root/'真实OCR 测试.mp4'
        im = Image.new('RGB',(540,720),'black')
        draw = ImageDraw.Draw(im)
        draw.text((80,430),'WD 20000',fill='white',font=ImageFont.load_default(size=42))
        frame = cv2.cvtColor(np.asarray(im),cv2.COLOR_RGB2BGR)
        writer = cv2.VideoWriter(str(source),cv2.VideoWriter_fourcc(*'mp4v'),10,(540,720))
        self.assertTrue(writer.isOpened())
        for _ in range(12):
            writer.write(frame)
        writer.release()
        config = {**self.config, 'roi':[0,.5,1,.8]}
        engine = Engines(config,lambda _:None)
        result = engine.extract_ocr(source,self.root/'ocr',1.2)
        self.assertGreater(len(result['frames']),2)
        self.assertTrue(any(f['reused_identical_frame'] for f in result['frames']))
        self.assertTrue(any('WD' in s['text'] for s in result['segments']))
        atomic(self.root/'ocr/result.json',json.dumps(result,ensure_ascii=False))
    def test_cuda_model_failure_falls_back_to_cpu(self):
        from laobu.media import Engines
        config = {**self.config,'device':'cuda','model_dir':str(self.root)}
        calls = []
        def create(*args,**kwargs):
            calls.append(kwargs['device'])
            if kwargs['device']=='cuda':
                raise RuntimeError('injected missing CUDA library')
            return object()
        engine = Engines(config,lambda _:None)
        with patch('faster_whisper.WhisperModel',side_effect=create):
            engine.load_model()
        self.assertEqual(['cuda','cpu'],calls)
        self.assertEqual('cpu',engine.device)
        self.assertIn('CUDA',engine.fallback)
    def test_contract_checks_nested_types_references_and_truthful_status(self):
        from laobu.contract import validate_contract
        schema = {'type':'object','required':['segments','status'],'properties':{'segments':{'type':'array','items':{'$ref':'#/$defs/s'}},'status':{'const':'unreviewed'}},'$defs':{'s':{'type':'object','required':['start'],'properties':{'start':{'type':'number','minimum':0}}}}}
        self.assertEqual([],validate_contract({'segments':[{'start':0}],'status':'unreviewed'},schema))
        self.assertEqual(3,len(validate_contract({'segments':[{'start':-1},{'start':True}],'status':'reviewed'},schema)))
    def test_human_candidate_requires_explicit_apply_and_complete_range(self):
        import copy
        from laobu.reviews import apply_human_candidates
        packet = self.packet('LB-1')
        original = copy.deepcopy(packet['asr'])
        note = {'id':1,'kind':'human_review','review_id':'r1','start':0.,'end':1.,'text':'听核修订','author':'用户','evidence_json':json.dumps({'apply_transcript':True,'description':'已回听'})}
        updated = apply_human_candidates(copy.deepcopy(packet),[note],{'r1':'resolved'})
        self.assertEqual('听核修订',updated['candidate_segments'][0]['text'])
        self.assertEqual(original,updated['asr'])
        self.assertEqual('human_confirmed',updated['alignments'][0]['human_review_status'])
        for changed_note,status in [(dict(note,start=.5),'resolved'),(dict(note,evidence_json='{}'),'resolved'),(note,'pending')]:
            result = apply_human_candidates(copy.deepcopy(packet),[changed_note],{'r1':status})
            self.assertNotIn('candidate_segments',result)

if __name__ == "__main__":
    unittest.main(verbosity=2)
