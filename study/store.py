"""规范化序列化、内容哈希、只增日志与可复现校验。

所有领域对象都能被规范化（canonical）为稳定的 JSON 文本，
其 SHA-256 即内容哈希。日志只增：每次状态变化追加一条快照，
verify_journal 可把日志折叠后与当前状态逐对象比对。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum


def to_jsonable(obj):
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(x) for x in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted(to_jsonable(x) for x in obj)
    return obj


def canonical(obj) -> str:
    return json.dumps(to_jsonable(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def hash_of(obj) -> str:
    """对象内容哈希；自带 content_hash 字段的对象只哈希其内容字段。"""
    if is_dataclass(obj) and not isinstance(obj, type) and any(
        f.name == "content_hash" for f in fields(obj)
    ):
        data = {f.name: getattr(obj, f.name) for f in fields(obj) if f.name != "content_hash"}
        return content_hash(data)
    return content_hash(obj)


@dataclass(frozen=True)
class JournalEntry:
    seq: int
    ts_ms: int
    actor: str
    action: str
    entity_kind: str
    entity_id: str
    state_hash: str
    state: dict


@dataclass
class Store:
    """全部领域状态。原始流、同意、假设等一经写入不得修改或删除。"""

    participants: dict = field(default_factory=dict)
    identities: dict = field(default_factory=dict)
    consents: dict = field(default_factory=dict)      # pid -> {version: ConsentRecord}
    electrodes: dict = field(default_factory=dict)    # pid -> {version: ElectrodeLocalization}
    stimulus_sets: dict = field(default_factory=dict)  # version -> StimulusSet
    clinical_blocks: dict = field(default_factory=dict)
    sessions: dict = field(default_factory=dict)
    streams: dict = field(default_factory=dict)
    corrections: dict = field(default_factory=dict)
    segments: dict = field(default_factory=dict)
    alignments: dict = field(default_factory=dict)
    exclusions: dict = field(default_factory=dict)
    hypotheses: dict = field(default_factory=dict)
    analyses: dict = field(default_factory=dict)
    safety_events: dict = field(default_factory=dict)
    publications: dict = field(default_factory=dict)
    quarantined_streams: set = field(default_factory=set)  # 按同意决定隔离的原始流
    journal: list = field(default_factory=list)


def live_hashes(store: Store) -> dict:
    """当前状态逐对象的内容哈希，键为 实体类别 -> {id: hash}。"""
    return {
        "participant": {pid: hash_of(p) for pid, p in store.participants.items()},
        "identity": {pid: hash_of(i) for pid, i in store.identities.items()},
        "consent": {
            f"{pid}#v{v}": hash_of(c)
            for pid, versions in store.consents.items()
            for v, c in versions.items()
        },
        "electrode": {
            f"{pid}#v{v}": hash_of(e)
            for pid, versions in store.electrodes.items()
            for v, e in versions.items()
        },
        "stimulus": {v: hash_of(s) for v, s in store.stimulus_sets.items()},
        "clinical_block": {bid: hash_of(b) for bid, b in store.clinical_blocks.items()},
        "session": {sid: hash_of(s) for sid, s in store.sessions.items()},
        "stream": {sid: hash_of(s) for sid, s in store.streams.items()},
        "correction": {cid: hash_of(c) for cid, c in store.corrections.items()},
        "segment": {sid: hash_of(s) for sid, s in store.segments.items()},
        "alignment": {aid: hash_of(a) for aid, a in store.alignments.items()},
        "exclusion": {eid: hash_of(e) for eid, e in store.exclusions.items()},
        "hypothesis": {hid: hash_of(h) for hid, h in store.hypotheses.items()},
        "analysis": {aid: hash_of(a) for aid, a in store.analyses.items()},
        "safety_event": {eid: hash_of(e) for eid, e in store.safety_events.items()},
        "publication": {bid: hash_of(b) for bid, b in store.publications.items()},
    }


def verify_journal(store: Store) -> list:
    """把只增日志折叠后与当前状态比对，返回不一致项（空列表表示可复现）。"""
    folded = {}
    quarantine = set()
    for entry in store.journal:
        if entry.entity_kind == "quarantine":
            quarantine.add(entry.entity_id)
            continue
        folded.setdefault(entry.entity_kind, {})[entry.entity_id] = entry.state_hash

    mismatches = []
    live = live_hashes(store)
    for kind, ids in folded.items():
        for entity_id, state_hash in ids.items():
            if live.get(kind, {}).get(entity_id) != state_hash:
                mismatches.append(f"{kind}:{entity_id} 与日志不一致")
    for kind, ids in live.items():
        for entity_id in ids:
            if entity_id not in folded.get(kind, {}):
                mismatches.append(f"{kind}:{entity_id} 缺少日志记录")
    if quarantine != store.quarantined_streams:
        mismatches.append("quarantine 与日志不一致")
    return mismatches
