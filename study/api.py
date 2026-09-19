"""HTTP JSON 接口：把领域核心暴露为 REST 风格路由。

角色通过请求头声明（原型约定，真实部署应替换为认证层）：
    X-Actor-Role: coordinator | clinician | analyst
    X-Actor-Id:   操作者标识
"""

from __future__ import annotations

import json
import re
import threading

from . import access
from .errors import (
    AccessDenied,
    ClinicalConflict,
    ConsentViolation,
    DomainError,
    GovernanceError,
    ImmutableViolation,
    NotFound,
    StateError,
    ValidationError,
)
from .model import Actor, AnalysisZone, DataRange, Role
from .store import to_jsonable
from .system import StudySystem


def _need(body: dict, *names: str) -> list:
    missing = [name for name in names if body.get(name) is None]
    if missing:
        raise ValidationError("缺少字段: " + ", ".join(missing))
    return [body[name] for name in names]


def _data_range(raw) -> DataRange:
    if not isinstance(raw, dict):
        raise ValidationError("data_range 必须是对象")
    try:
        return DataRange.of(
            raw["participant_ids"], raw["session_window"], raw["stimulus_versions"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError(f"数据范围非法: {exc}") from exc


def _zone(raw):
    if raw is None:
        return None
    try:
        return AnalysisZone(raw)
    except ValueError as exc:
        raise ValidationError(f"分析分区非法: {raw}") from exc


class Api:
    """薄路由层：解析请求、调用领域核心、映射错误。"""

    def __init__(self, system: StudySystem):
        self.system = system
        self._lock = threading.Lock()
        self._routes = []
        add = self._add
        add("POST", r"^/participants$", self._create_participant)
        add("POST", r"^/participants/(?P<pid>[^/]+)/consents$", self._add_consent)
        add("POST", r"^/participants/(?P<pid>[^/]+)/electrodes$", self._add_electrodes)
        add("POST", r"^/participants/(?P<pid>[^/]+)/clinical-blocks$", self._add_clinical_block)
        add("POST", r"^/participants/(?P<pid>[^/]+)/sessions$", self._schedule_session)
        add("POST", r"^/participants/(?P<pid>[^/]+)/adverse-events$", self._adverse_event)
        add("POST", r"^/participants/(?P<pid>[^/]+)/order-changes$", self._order_change)
        add("POST", r"^/participants/(?P<pid>[^/]+)/pause$", self._pause)
        add("POST", r"^/participants/(?P<pid>[^/]+)/resume$", self._resume)
        add("GET", r"^/participants/(?P<pid>[^/]+)/safety$", self._safety_view)
        add("GET", r"^/participants/(?P<pid>[^/]+)$", self._participant_view)
        add("POST", r"^/stimulus-sets$", self._create_stimulus)
        add("POST", r"^/sessions/(?P<sid>[^/]+)/start$", self._start_session)
        add("POST", r"^/sessions/(?P<sid>[^/]+)/complete$", self._complete_session)
        add("POST", r"^/sessions/(?P<sid>[^/]+)/streams$", self._ingest_stream)
        add("POST", r"^/streams/(?P<sid>[^/]+)/clock-corrections$", self._add_correction)
        add("POST", r"^/streams/(?P<sid>[^/]+)/segments$", self._add_segment)
        add("GET", r"^/segments/(?P<sid>[^/]+)/extract$", self._extract_segment)
        add("POST", r"^/alignments$", self._create_alignment)
        add("GET", r"^/alignments/(?P<aid>[^/]+)/run$", self._run_alignment)
        add("POST", r"^/hypotheses$", self._register_hypothesis)
        add("POST", r"^/analyses$", self._run_analysis)
        add("GET", r"^/analyses/(?P<aid>[^/]+)$", self._analysis_view)
        add("POST", r"^/exclusions$", self._record_exclusion)
        add("POST", r"^/publications$", self._build_publication)
        add("GET", r"^/publications/(?P<bid>[^/]+)/verify$", self._verify_publication)
        add("GET", r"^/journal/verify$", self._verify_journal)

    def _add(self, method, pattern, handler):
        self._routes.append((method, re.compile(pattern), handler))

    # ------------------------------------------------------------------
    def handle(self, method: str, path: str, headers, raw_body: bytes):
        with self._lock:
            try:
                matched = None
                for route_method, pattern, handler in self._routes:
                    if route_method != method:
                        continue
                    match = pattern.match(path)
                    if match:
                        matched = (handler, match.groupdict())
                        break
                if matched is None:
                    raise NotFound(f"未知路由 {method} {path}")
                actor = self._actor(headers)
                body = self._parse_body(raw_body)
                handler, groups = matched
                result = handler(actor, body, **groups)
                return 200, {"ok": True, "data": to_jsonable(result)}
            except AccessDenied as exc:
                return 403, self._error(exc)
            except NotFound as exc:
                return 404, self._error(exc)
            except ValidationError as exc:
                return 400, self._error(exc)
            except (
                ClinicalConflict,
                ConsentViolation,
                GovernanceError,
                ImmutableViolation,
                StateError,
            ) as exc:
                return 409, self._error(exc)
            except DomainError as exc:
                return 400, self._error(exc)
            except Exception as exc:  # 未预期错误：不断开连接，不泄露内部细节
                return 500, {"ok": False, "error": "InternalError", "message": str(exc)}

    @staticmethod
    def _error(exc: Exception) -> dict:
        return {"ok": False, "error": type(exc).__name__, "message": str(exc)}

    @staticmethod
    def _actor(headers) -> Actor:
        role_raw = headers.get("X-Actor-Role")
        if not role_raw:
            raise AccessDenied("缺少 X-Actor-Role 请求头")
        try:
            role = Role(role_raw)
        except ValueError as exc:
            raise AccessDenied(f"未知角色: {role_raw}") from exc
        return Actor(role=role, actor_id=headers.get("X-Actor-Id") or "anonymous")

    @staticmethod
    def _parse_body(raw_body: bytes) -> dict:
        if not raw_body:
            return {}
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("请求体不是合法 JSON") from exc
        if not isinstance(body, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return body

    # ------------------------------------------------------------------
    # 登记
    # ------------------------------------------------------------------
    def _create_participant(self, actor, body):
        participant_id, identity, start, end = _need(
            body, "participant_id", "identity", "inpatient_start_ms", "inpatient_end_ms"
        )
        self.system.enroll_participant(actor, participant_id, identity, start, end)
        return {"participant_id": participant_id}

    def _add_consent(self, actor, body, pid):
        scopes, effective_from = _need(body, "scopes", "effective_from_ms")
        record = self.system.record_consent(
            actor, pid, scopes, effective_from,
            body.get("effective_to_ms"),
            body.get("completed_data_policy", "retain"),
        )
        return {"participant_id": pid, "version": record.version}

    def _add_electrodes(self, actor, body, pid):
        contacts, method, effective_ms = _need(body, "contacts", "method", "effective_ms")
        record = self.system.record_electrodes(actor, pid, contacts, method, effective_ms)
        return {"participant_id": pid, "version": record.version}

    def _create_stimulus(self, actor, body):
        version, trial_types, timing = _need(body, "version", "trial_types", "timing")
        stimulus = self.system.register_stimulus_set(actor, version, trial_types, timing)
        return {"version": stimulus.version, "content_hash": stimulus.content_hash}

    # ------------------------------------------------------------------
    # 排程与环节
    # ------------------------------------------------------------------
    def _add_clinical_block(self, actor, body, pid):
        kind, start_ms, end_ms = _need(body, "kind", "start_ms", "end_ms")
        block, invalidated = self.system.add_clinical_block(
            actor, pid, kind, start_ms, end_ms,
            order_id=body.get("order_id"), note=body.get("note", ""),
        )
        return {"block_id": block.block_id, "invalidated_sessions": invalidated}

    def _schedule_session(self, actor, body, pid):
        start_ms, end_ms, stimulus_version = _need(
            body, "start_ms", "end_ms", "stimulus_version"
        )
        session = self.system.schedule_session(actor, pid, start_ms, end_ms, stimulus_version)
        return {"session_id": session.session_id}

    def _start_session(self, actor, body, sid):
        session = self.system.start_session(actor, sid)
        return {"session_id": session.session_id, "status": session.status}

    def _complete_session(self, actor, body, sid):
        session = self.system.complete_session(actor, sid)
        return {"session_id": session.session_id, "status": session.status}

    # ------------------------------------------------------------------
    # 原始流、校正、分段、对齐
    # ------------------------------------------------------------------
    def _ingest_stream(self, actor, body, sid):
        device, clock_id, started_ms, records = _need(
            body, "device", "clock_id", "started_ms", "records"
        )
        stream = self.system.ingest_stream(actor, sid, device, clock_id, started_ms, records)
        return {"stream_id": stream.stream_id, "content_hash": stream.content_hash}

    def _add_correction(self, actor, body, sid):
        offset_ms, drift_ppm, anchors, reason = _need(
            body, "offset_ms", "drift_ppm", "anchors", "reason"
        )
        correction = self.system.add_clock_correction(
            actor, sid, offset_ms, drift_ppm, anchors, reason
        )
        return {"correction_id": correction.correction_id}

    def _add_segment(self, actor, body, sid):
        label, correction_id, start_ms, end_ms = _need(
            body, "label", "correction_id", "start_device_ms", "end_device_ms"
        )
        segment = self.system.add_segment(
            actor, sid, label, correction_id, start_ms, end_ms
        )
        return {"segment_id": segment.segment_id}

    def _extract_segment(self, actor, body, sid):
        return self.system.extract_segment(actor, sid)

    def _create_alignment(self, actor, body):
        behavior_stream, neural_stream, behavior_corr, neural_corr = _need(
            body,
            "behavior_stream_id", "neural_stream_id",
            "behavior_correction_id", "neural_correction_id",
        )
        alignment, output = self.system.align_events(
            actor, behavior_stream, neural_stream, behavior_corr, neural_corr
        )
        return {"alignment_id": alignment.alignment_id, "output": output}

    def _run_alignment(self, actor, body, aid):
        return self.system.run_alignment(actor, aid)

    # ------------------------------------------------------------------
    # 治理
    # ------------------------------------------------------------------
    def _register_hypothesis(self, actor, body):
        statement, data_range, plan, code_version = _need(
            body, "statement", "data_range", "analysis_plan", "code_version"
        )
        hypothesis = self.system.register_hypothesis(
            actor, statement, _data_range(data_range), plan, code_version
        )
        return {
            "hypothesis_id": hypothesis.hypothesis_id,
            "registered_ms": hypothesis.registered_ms,
        }

    def _run_analysis(self, actor, body):
        data_range, code_version, electrode_versions, parameters, result_summary = _need(
            body, "data_range", "code_version", "electrode_versions",
            "parameters", "result_summary",
        )
        analysis = self.system.run_analysis(
            actor,
            _data_range(data_range),
            code_version,
            electrode_versions,
            parameters,
            result_summary,
            exclusion_ids=body.get("exclusion_ids", ()),
            alignment_ids=body.get("alignment_ids", ()),
            hypothesis_id=body.get("hypothesis_id"),
            supersedes=body.get("supersedes"),
            zone=_zone(body.get("zone")),
        )
        return {
            "analysis_id": analysis.analysis_id,
            "version": analysis.version,
            "zone": analysis.zone,
        }

    def _analysis_view(self, actor, body, aid):
        return access.analysis_view(self.system, actor, aid)

    def _record_exclusion(self, actor, body):
        target_kind, target_id, reason, code = _need(
            body, "target_kind", "target_id", "reason", "code"
        )
        exclusion = self.system.record_exclusion(actor, target_kind, target_id, reason, code)
        return {"exclusion_id": exclusion.exclusion_id}

    def _build_publication(self, actor, body):
        analysis_ids, conclusions = _need(body, "analysis_ids", "conclusions")
        bundle = self.system.build_publication(actor, analysis_ids, conclusions)
        return {"bundle_id": bundle.bundle_id}

    def _verify_publication(self, actor, body, bid):
        return self.system.verify_publication(actor, bid)

    # ------------------------------------------------------------------
    # 安全联动
    # ------------------------------------------------------------------
    def _adverse_event(self, actor, body, pid):
        severity, description, occurred_ms = _need(
            body, "severity", "description", "occurred_ms"
        )
        event, affected = self.system.record_adverse_event(
            actor, pid, severity, description, occurred_ms
        )
        return {"event_id": event.event_id, "affected_sessions": affected}

    def _order_change(self, actor, body, pid):
        order_id, note, effective_ms = _need(body, "order_id", "note", "effective_ms")
        event, invalidated = self.system.record_order_change(
            actor, pid, order_id, note, effective_ms,
            suspend_research_until_ms=body.get("suspend_research_until_ms"),
            blocks=body.get("blocks", ()),
        )
        return {"event_id": event.event_id, "invalidated_sessions": invalidated}

    def _pause(self, actor, body, pid):
        (reason,) = _need(body, "reason")
        event, invalidated, quarantined = self.system.pause_participant(actor, pid, reason)
        return {
            "event_id": event.event_id,
            "invalidated_sessions": invalidated,
            "quarantined_streams": quarantined,
        }

    def _resume(self, actor, body, pid):
        participant = self.system.resume_participant(actor, pid)
        return {"participant_id": participant.participant_id, "status": participant.status}

    # ------------------------------------------------------------------
    # 视图与审计
    # ------------------------------------------------------------------
    def _participant_view(self, actor, body, pid):
        return access.participant_view(self.system, actor, pid)

    def _safety_view(self, actor, body, pid):
        return access.safety_view(self.system, actor, pid)

    def _verify_journal(self, actor, body):
        return {"mismatches": self.system.verify_journal(actor)}
