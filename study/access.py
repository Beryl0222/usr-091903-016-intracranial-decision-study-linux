"""按角色的访问视图。

最小知情原则：
- 分析人员看不到患者直接身份（假名编号代替）；
- 临床人员可查看安全信息，但不接触未授权的研究推断
  （分析结果、假设、发布结论）；
- 协调员掌握排程与登记所需的运行信息。
"""

from __future__ import annotations

from .errors import AccessDenied, NotFound
from .model import Actor, Role
from .store import to_jsonable


def _session_safety(session) -> dict:
    """临床可见的环节安全状态（不含研究内容）。"""
    return {
        "session_id": session.session_id,
        "start_ms": session.start_ms,
        "end_ms": session.end_ms,
        "status": session.status.value,
        "invalidation": session.invalidation,
    }


def _session_research(session, store) -> dict:
    """研究侧的环节全貌。"""
    data = _session_safety(session)
    data.update(
        {
            "stimulus_version": session.stimulus_version,
            "electrode_version": session.electrode_version,
            "consent_version": session.consent_version,
            "started_ms": session.started_ms,
            "completed_ms": session.completed_ms,
            "stream_ids": list(session.stream_ids),
            "quarantined_streams": [
                sid for sid in session.stream_ids if sid in store.quarantined_streams
            ],
        }
    )
    return data


def _sessions_of(store, participant_id):
    return [s for s in store.sessions.values() if s.participant_id == participant_id]


def _clinical_blocks_of(store, participant_id):
    return [b for b in store.clinical_blocks.values() if b.participant_id == participant_id]


def _safety_events_of(store, participant_id):
    return [e for e in store.safety_events.values() if e.participant_id == participant_id]


def participant_view(system, actor: Actor, participant_id: str) -> dict:
    """参与者视图：按角色返回不同投影。"""
    store = system.store
    participant = store.participants.get(participant_id)
    if participant is None:
        raise NotFound(f"参与者不存在: {participant_id}")
    base = {
        "participant_id": participant.participant_id,
        "status": participant.status.value,
        "inpatient_window": [participant.inpatient_start_ms, participant.inpatient_end_ms],
    }

    if actor.role == Role.ANALYST:
        # 假名编号视图：绝不包含直接身份。
        base.update(
            {
                "consents": [
                    {
                        "version": c.version,
                        "scopes": sorted(s.value for s in c.scopes),
                        "effective_from_ms": c.effective_from_ms,
                        "effective_to_ms": c.effective_to_ms,
                        "completed_data_policy": c.completed_data_policy.value,
                    }
                    for c in sorted(
                        store.consents.get(participant_id, {}).values(),
                        key=lambda c: c.version,
                    )
                ],
                "electrode_versions": [
                    to_jsonable(e)
                    for e in sorted(
                        store.electrodes.get(participant_id, {}).values(),
                        key=lambda e: e.version,
                    )
                ],
                "sessions": [
                    _session_research(s, store)
                    for s in _sessions_of(store, participant_id)
                ],
                "quarantined_streams": sorted(store.quarantined_streams),
            }
        )
        return base

    if actor.role == Role.CLINICIAN:
        identity = store.identities[participant_id]
        base.update(
            {
                "identity": to_jsonable(identity),
                "clinical_blocks": [
                    to_jsonable(b) for b in _clinical_blocks_of(store, participant_id)
                ],
                "sessions": [
                    _session_safety(s) for s in _sessions_of(store, participant_id)
                ],
            }
        )
        return base

    # 协调员：排程与登记所需的运行信息。
    identity = store.identities[participant_id]
    base.update(
        {
            "identity": to_jsonable(identity),
            "consents": [
                to_jsonable(c)
                for c in sorted(
                    store.consents.get(participant_id, {}).values(), key=lambda c: c.version
                )
            ],
            "electrode_versions": [
                to_jsonable(e)
                for e in sorted(
                    store.electrodes.get(participant_id, {}).values(), key=lambda e: e.version
                )
            ],
            "clinical_blocks": [
                to_jsonable(b) for b in _clinical_blocks_of(store, participant_id)
            ],
            "sessions": [
                _session_research(s, store) for s in _sessions_of(store, participant_id)
            ],
        }
    )
    return base


def safety_view(system, actor: Actor, participant_id: str) -> dict:
    """临床安全视图：仅临床人员可见，不含任何研究推断。"""
    if actor.role != Role.CLINICIAN:
        raise AccessDenied("安全视图仅临床人员可查看")
    store = system.store
    participant = store.participants.get(participant_id)
    if participant is None:
        raise NotFound(f"参与者不存在: {participant_id}")
    identity = store.identities[participant_id]
    return {
        "participant_id": participant_id,
        "identity": to_jsonable(identity),
        "status": participant.status.value,
        "inpatient_window": [participant.inpatient_start_ms, participant.inpatient_end_ms],
        "clinical_blocks": [
            to_jsonable(b) for b in _clinical_blocks_of(store, participant_id)
        ],
        "safety_events": [
            to_jsonable(e) for e in _safety_events_of(store, participant_id)
        ],
        "sessions": [_session_safety(s) for s in _sessions_of(store, participant_id)],
    }


def analysis_view(system, actor: Actor, analysis_id: str) -> dict:
    """研究推断（分析结果）仅分析人员可见。"""
    if actor.role != Role.ANALYST:
        raise AccessDenied("研究推断仅分析人员可查看")
    analysis = system.store.analyses.get(analysis_id)
    if analysis is None:
        raise NotFound(f"分析不存在: {analysis_id}")
    data = to_jsonable(analysis)
    if analysis.hypothesis_id:
        hypothesis = system.store.hypotheses.get(analysis.hypothesis_id)
        if hypothesis is not None:
            data["hypothesis_statement"] = hypothesis.statement
    return data
