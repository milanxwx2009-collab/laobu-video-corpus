"""Apply only explicitly resolved human reviews to a separate candidate channel."""
import copy
import json

def apply_human_candidates(packet, annotations, review_statuses):
    candidates = copy.deepcopy(packet.get('candidate_segments') or packet['asr']['segments'])
    latest = {}
    for note in annotations:
        if note['kind']=='human_review' and note.get('review_id'):
            latest[note['review_id']] = note
    history = []
    for rid,note in latest.items():
        if review_statuses.get(rid)!='resolved':
            continue
        if not json.loads(note['evidence_json']).get('apply_transcript',False):
            continue
        start,end = note['start'],note['end']
        indices = [i for i,s in enumerate(candidates) if min(s['end'],end)-max(s['start'],start)>.01]
        # A review spanning only part of a cue cannot be split without guessing timing.
        applied = bool(indices) and abs(candidates[indices[0]]['start']-start)<.11 and abs(candidates[indices[-1]]['end']-end)<.11
        entry = {'annotation_id':note['id'],'review_id':rid,'start':start,'end':end,'text':note['text'],'author':note['author'],'basis':json.loads(note['evidence_json']),'applied_to_candidate':applied,'source_cue_ids':[candidates[i]['id'] for i in indices],'note':'仅应用用户明确resolved且边界匹配的核对；无法安全分配时间则独立保留意见'}
        history.append(entry)
        if applied:
            replacement = {'id':f"HUMAN-{note['id']}",'start':start,'end':end,'text':note['text'],'human_review_status':'human_confirmed','channel':'human_review_candidate','annotation_id':note['id'],'author':note['author'],'revision_basis':entry['basis'],'revision_history':[entry]}
            candidates[indices[0]:indices[-1]+1] = [replacement]
            for a in packet['alignments']:
                if abs(a['start']-start)<.11 and abs(a['end']-end)<.11:
                    a['candidate_transcript'] = note['text']
                    a['human_review_status'] = 'human_confirmed'
                    a['revision_basis'] = f"人工核对记录{note['id']}，核对者{note['author']}"
                    a['revision_history'].append(entry)
    if any(h['applied_to_candidate'] for h in history):
        packet['candidate_segments'] = candidates
        packet['candidate_basis'] = 'human_review_overlay_on_preserved_source'
    packet['manual_candidate_revisions'] = history
    return packet
