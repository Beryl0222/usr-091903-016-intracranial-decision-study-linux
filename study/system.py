"""研究协调系统的领域核心。

贯穿全局的四条原则：

1. 临床优先：临床占时不可移动，研究环节只能使用空档；
   新的临床安排出现时，与之冲突的未开始环节立即失效。
2. 原始流不可变：时钟校正与数据分段都是派生层，引用而不改动原始流。
3. 预注册治理：验证性分析必须引用预注册假设与固定数据范围；
   临时试出的结果只能留在探索区，不能作为发布依据。
4. 安全联动：不良反应、医嘱变化、参与者暂停立即使未开始环节失效，
   已完成数据按同意决定处理。
"""

from __future__ import annotations

import time
from typing import Optional

from .errors import (
    AccessDenied,
    ClinicalConflict,
    ConsentViolation,
    GovernanceError,
    ImmutableViolation,
    NotFound,
    StateError,
    ValidationError,
)
from .model import (
    REQUIRED_SESSION_SCOPES,
    Actor,
    Analysis,
    AnalysisZone,
    BlockKind,
    ClinicalBlock,
    ClockCorrection,
    CompletedDataPolicy,
    ConsentRecord,
    ConsentScope,
    Contact,
    DataRange,
    ElectrodeLocalization,
    EntityRef,
    EventAlignment,
    ExclusionRecord,
    Hypothesis,
    IdentityRecord,
    Participant,
    ParticipantStatus,
    PublicationBundle,
    RawStream,
    Role,
    Segment,
    Session,
    SessionStatus,
    Severity,
    SafetyEvent,
    StimulusSet,
    TrialType,
)
from .store import JournalEntry, Store, content_hash, hash_of, to_jsonable, verify_journal


def _default_now_ms() -> int:
    return int(time.time() * 1000)


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


class StudySystem:
    """协调住院深部电极患者风险决策研究的核心服务。"""

    def __init__(self, now_ms=None):
        self._now = now_ms or _default_now_ms
        self.store = Store()
        self._counters = {}

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------
    def _gen_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:04d}"

    def _journal(self, actor: Actor, action: str, kind: str, entity_id: str, obj) -> None:
        self.store.journal.append(
            JournalEntry(
                seq=len(self.store.journal),
                ts_ms=self._now(),
                actor=actor.actor_id,
                action=action,
                entity_kind=kind,
                entity_id=entity_id,
                state_hash=hash_of(obj),
                state=to_jsonable(obj),
            )
        )

    @staticmethod
    def _require(actor: Actor, *roles: Role) -> None:
        if actor.role not in roles:
            need = "/".join(r.value for r in roles)
            raise AccessDenied(f"角色 {actor.role.value} 无权执行此操作（需要 {need}）")

    def _participant(self, participant_id: str) -> Participant:
        participant = self.store.participants.get(participant_id)
        if participant is None:
            raise NotFound(f"参与者不存在: {participant_id}")
        return participant

    def _session(self, session_id: str) -> Session:
        session = self.store.sessions.get(session_id)
        if session is None:
            raise NotFound(f"环节不存在: {session_id}")
        return session

    def current_consent(self, participant_id: str, at_ms: int) -> Optional[ConsentRecord]:
        best = None
        for record in self.store.consents.get(participant_id, {}).values():
            in_window = record.effective_from_ms <= at_ms and (
                record.effective_to_ms is None or at_ms <= record.effective_to_ms
            )
            if in_window and (best is None or record.version > best.version):
                best = record
        return best

    # ------------------------------------------------------------------
    # 登记：参与者、同意、电极、刺激材料
    # ------------------------------------------------------------------
    def enroll_participant(
        self, actor: Actor, participant_id: str, identity: dict,
        inpatient_start_ms: int, inpatient_end_ms: int,
    ) -> Participant:
        """登记参与者。直接身份进入隔离的身份库，研究侧只用假名编号。"""
        self._require(actor, Role.COORDINATOR)
        if participant_id in self.store.participants:
            raise ValidationError(f"参与者编号已存在: {participant_id}")
        if inpatient_start_ms >= inpatient_end_ms:
            raise ValidationError("住院窗口不合法")
        for key in ("legal_name", "medical_record_number"):
            if not identity.get(key):
                raise ValidationError(f"身份缺少字段: {key}")
        participant = Participant(participant_id, inpatient_start_ms, inpatient_end_ms)
        record = IdentityRecord(
            participant_id, identity["legal_name"], identity["medical_record_number"]
        )
        self.store.participants[participant_id] = participant
        self.store.identities[participant_id] = record
        self._journal(actor, "enroll", "participant", participant_id, participant)
        self._journal(actor, "enroll", "identity", participant_id, record)
        return participant

    def record_consent(
        self, actor: Actor, participant_id: str, scopes,
        effective_from_ms: int, effective_to_ms: Optional[int],
        completed_data_policy,
    ) -> ConsentRecord:
        """追加一版同意记录；旧版本保留，永不改写。"""
        self._require(actor, Role.COORDINATOR)
        self._participant(participant_id)
        try:
            scope_set = frozenset(
                s if isinstance(s, ConsentScope) else ConsentScope(s) for s in scopes
            )
            policy = (
                completed_data_policy
                if isinstance(completed_data_policy, CompletedDataPolicy)
                else CompletedDataPolicy(completed_data_policy)
            )
        except ValueError as exc:
            raise ValidationError(f"同意内容非法: {exc}") from exc
        if not scope_set:
            raise ValidationError("同意范围不能为空")
        if effective_to_ms is not None and effective_to_ms <= effective_from_ms:
            raise ValidationError("同意生效窗口不合法")
        versions = self.store.consents.setdefault(participant_id, {})
        record = ConsentRecord(
            participant_id=participant_id,
            version=len(versions) + 1,
            scopes=scope_set,
            effective_from_ms=effective_from_ms,
            effective_to_ms=effective_to_ms,
            completed_data_policy=policy,
            recorded_ms=self._now(),
            recorded_by=actor.actor_id,
        )
        versions[record.version] = record
        self._journal(actor, "consent", "consent", f"{participant_id}#v{record.version}", record)
        return record

    def record_electrodes(
        self, actor: Actor, participant_id: str, contacts, method: str, effective_ms: int
    ) -> ElectrodeLocalization:
        """登记一版电极位置（如术后 CT 重新配准）。"""
        self._require(actor, Role.COORDINATOR, Role.CLINICIAN)
        self._participant(participant_id)
        if not contacts:
            raise ValidationError("触点列表不能为空")
        try:
            contact_tuple = tuple(
                Contact(
                    contact_id=str(c["contact_id"]),
                    region=str(c["region"]),
                    x=float(c["x"]),
                    y=float(c["y"]),
                    z=float(c["z"]),
                )
                for c in contacts
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(f"触点描述非法: {exc}") from exc
        versions = self.store.electrodes.setdefault(participant_id, {})
        record = ElectrodeLocalization(
            participant_id=participant_id,
            version=len(versions) + 1,
            contacts=contact_tuple,
            method=method,
            effective_ms=effective_ms,
        )
        versions[record.version] = record
        self._journal(actor, "electrodes", "electrode", f"{participant_id}#v{record.version}", record)
        return record

    def register_stimulus_set(self, actor: Actor, version: str, trial_types, timing) -> StimulusSet:
        """注册刺激材料版本（宝石/炸弹概率与毫秒级时序），版本内不可变。"""
        self._require(actor, Role.COORDINATOR)
        if version in self.store.stimulus_sets:
            raise ImmutableViolation(f"刺激材料版本不可变，已存在: {version}")
        if not trial_types:
            raise ValidationError("试次类型不能为空")
        parsed = []
        for t in trial_types:
            try:
                gem = float(t["gem_probability"])
                bomb = float(t["bomb_probability"])
                name = str(t["name"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValidationError(f"试次类型非法: {exc}") from exc
            if not (0.0 <= gem <= 1.0) or not (0.0 <= bomb <= 1.0):
                raise ValidationError("宝石/炸弹概率必须在 [0, 1] 内")
            parsed.append(TrialType(name=name, gem_probability=gem, bomb_probability=bomb))
        if not isinstance(timing, dict) or not timing:
            raise ValidationError("时序参数不能为空")
        for key, value in timing.items():
            if not isinstance(value, (int, float)) or value <= 0:
                raise ValidationError(f"时序参数必须为正数毫秒: {key}")
        stimulus = StimulusSet(
            version=version,
            trial_types=tuple(parsed),
            timing=dict(timing),
            content_hash="",
        )
        stimulus = StimulusSet(
            version=stimulus.version,
            trial_types=stimulus.trial_types,
            timing=stimulus.timing,
            content_hash=hash_of(stimulus),
        )
        self.store.stimulus_sets[version] = stimulus
        self._journal(actor, "stimulus", "stimulus", version, stimulus)
        return stimulus

    # ------------------------------------------------------------------
    # 排程：临床优先
    # ------------------------------------------------------------------
    def add_clinical_block(
        self, actor: Actor, participant_id: str, kind,
        start_ms: int, end_ms: int, order_id: Optional[str] = None, note: str = "",
    ):
        """登记临床占时；与之冲突的未开始环节立即失效（临床优先）。"""
        self._require(actor, Role.CLINICIAN)
        self._participant(participant_id)
        try:
            block_kind = kind if isinstance(kind, BlockKind) else BlockKind(kind)
        except ValueError as exc:
            raise ValidationError(f"临床占时类型非法: {exc}") from exc
        if start_ms >= end_ms:
            raise ValidationError("临床占时窗口不合法")
        block = ClinicalBlock(
            block_id=self._gen_id("blk"),
            participant_id=participant_id,
            kind=block_kind,
            start_ms=start_ms,
            end_ms=end_ms,
            order_id=order_id,
            note=note,
            recorded_ms=self._now(),
            recorded_by=actor.actor_id,
        )
        self.store.clinical_blocks[block.block_id] = block
        self._journal(actor, "clinical-block", "clinical_block", block.block_id, block)
        invalidated = self._invalidate_conflicting(
            actor, participant_id, start_ms, end_ms, f"clinical-priority:{block.block_id}"
        )
        return block, invalidated

    def _invalidate_conflicting(
        self, actor: Actor, participant_id: str, start_ms: int, end_ms: int, reason: str
    ) -> list:
        invalidated = []
        for session in self.store.sessions.values():
            if (
                session.participant_id == participant_id
                and session.status == SessionStatus.PLANNED
                and _overlaps(session.start_ms, session.end_ms, start_ms, end_ms)
            ):
                self._invalidate_session(actor, session, reason)
                invalidated.append(session.session_id)
        return invalidated

    def _invalidate_session(self, actor: Actor, session: Session, reason: str) -> None:
        session.status = SessionStatus.INVALIDATED
        session.invalidation = reason
        self._journal(actor, "invalidate", "session", session.session_id, session)

    def _abort_session(self, actor: Actor, session: Session, reason: str) -> None:
        session.status = SessionStatus.ABORTED
        session.invalidation = reason
        self._journal(actor, "abort", "session", session.session_id, session)

    def schedule_session(
        self, actor: Actor, participant_id: str, start_ms: int, end_ms: int, stimulus_version: str
    ) -> Session:
        """在临床空档内排程实验环节，并钉住刺激、电极与同意版本。"""
        self._require(actor, Role.COORDINATOR)
        participant = self._participant(participant_id)
        if participant.status != ParticipantStatus.ACTIVE:
            raise StateError(f"参与者状态为 {participant.status.value}，不能排程")
        if start_ms >= end_ms:
            raise ValidationError("环节窗口不合法")
        if start_ms < participant.inpatient_start_ms or end_ms > participant.inpatient_end_ms:
            raise ValidationError("环节超出住院窗口")
        consent = self.current_consent(participant_id, start_ms)
        if consent is None or not REQUIRED_SESSION_SCOPES <= consent.scopes:
            raise ConsentViolation("同意范围未覆盖行为与脑信号采集，不能排程")
        if stimulus_version not in self.store.stimulus_sets:
            raise NotFound(f"刺激材料版本不存在: {stimulus_version}")
        electrode_versions = self.store.electrodes.get(participant_id, {})
        if not electrode_versions:
            raise ValidationError("缺少电极定位版本，不能排程")
        for block in self.store.clinical_blocks.values():
            if block.participant_id == participant_id and _overlaps(
                start_ms, end_ms, block.start_ms, block.end_ms
            ):
                raise ClinicalConflict(f"与临床占时 {block.block_id} 冲突（临床优先）")
        for other in self.store.sessions.values():
            if (
                other.participant_id == participant_id
                and other.status in (SessionStatus.PLANNED, SessionStatus.IN_PROGRESS)
                and _overlaps(start_ms, end_ms, other.start_ms, other.end_ms)
            ):
                raise ValidationError(f"与已排程环节 {other.session_id} 重叠")
        session = Session(
            session_id=self._gen_id("ses"),
            participant_id=participant_id,
            start_ms=start_ms,
            end_ms=end_ms,
            stimulus_version=stimulus_version,
            electrode_version=max(electrode_versions),
            consent_version=consent.version,
        )
        self.store.sessions[session.session_id] = session
        self._journal(actor, "schedule", "session", session.session_id, session)
        return session

    def start_session(self, actor: Actor, session_id: str) -> Session:
        self._require(actor, Role.COORDINATOR)
        session = self._session(session_id)
        if session.status != SessionStatus.PLANNED:
            raise StateError(f"环节状态为 {session.status.value}，不能开始")
        participant = self._participant(session.participant_id)
        if participant.status != ParticipantStatus.ACTIVE:
            raise StateError("参与者不在可进行状态")
        for block in self.store.clinical_blocks.values():
            if block.participant_id == session.participant_id and _overlaps(
                session.start_ms, session.end_ms, block.start_ms, block.end_ms
            ):
                self._invalidate_session(actor, session, f"clinical-priority:{block.block_id}")
                raise ClinicalConflict(f"开始时与临床占时 {block.block_id} 冲突，环节已失效")
        consent = self.current_consent(session.participant_id, self._now())
        if consent is None or not REQUIRED_SESSION_SCOPES <= consent.scopes:
            raise ConsentViolation("开始时同意范围已不覆盖采集")
        session.status = SessionStatus.IN_PROGRESS
        session.started_ms = self._now()
        self._journal(actor, "start", "session", session.session_id, session)
        return session

    def complete_session(self, actor: Actor, session_id: str) -> Session:
        self._require(actor, Role.COORDINATOR)
        session = self._session(session_id)
        if session.status != SessionStatus.IN_PROGRESS:
            raise StateError(f"环节状态为 {session.status.value}，不能完成")
        session.status = SessionStatus.COMPLETED
        session.completed_ms = self._now()
        self._journal(actor, "complete", "session", session.session_id, session)
        return session

    # ------------------------------------------------------------------
    # 原始流、时钟校正、分段与事件对齐
    # ------------------------------------------------------------------
    def ingest_stream(
        self, actor: Actor, session_id: str, device: str, clock_id: str,
        started_ms: int, records,
    ) -> RawStream:
        """摄取原始流。写入后不可变：没有更新或删除入口。"""
        self._require(actor, Role.COORDINATOR)
        session = self._session(session_id)
        if session.status not in (SessionStatus.IN_PROGRESS, SessionStatus.COMPLETED):
            raise StateError(f"环节状态为 {session.status.value}，不能摄取数据")
        parsed = []
        previous_t = None
        for item in records:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ValidationError("记录必须是 (t_device_ms, payload) 对")
            t_device, payload = item
            if not isinstance(t_device, (int, float)) or t_device < 0:
                raise ValidationError("记录时间戳必须为非负毫秒")
            if previous_t is not None and t_device < previous_t:
                raise ValidationError("记录时间戳必须单调不减")
            previous_t = t_device
            parsed.append((t_device, payload))
        stream = RawStream(
            stream_id=self._gen_id("str"),
            session_id=session_id,
            participant_id=session.participant_id,
            device=device,
            clock_id=clock_id,
            started_ms=started_ms,
            records=tuple(parsed),
            content_hash="",
        )
        stream = RawStream(
            stream_id=stream.stream_id,
            session_id=stream.session_id,
            participant_id=stream.participant_id,
            device=stream.device,
            clock_id=stream.clock_id,
            started_ms=stream.started_ms,
            records=stream.records,
            content_hash=hash_of(stream),
        )
        self.store.streams[stream.stream_id] = stream
        session.stream_ids.append(stream.stream_id)
        self._journal(actor, "ingest", "stream", stream.stream_id, stream)
        self._journal(actor, "ingest", "session", session.session_id, session)
        return stream

    def add_clock_correction(
        self, actor: Actor, stream_id: str, offset_ms: float, drift_ppm: float,
        anchors, reason: str,
    ) -> ClockCorrection:
        """登记设备时钟校正。派生层：只引用原始流，绝不改动它。"""
        self._require(actor, Role.ANALYST, Role.COORDINATOR)
        stream = self.store.streams.get(stream_id)
        if stream is None:
            raise NotFound(f"原始流不存在: {stream_id}")
        anchor_tuple = tuple((int(a[0]), int(a[1])) for a in anchors)
        correction = ClockCorrection(
            correction_id=self._gen_id("clk"),
            stream_id=stream_id,
            offset_ms=float(offset_ms),
            drift_ppm=float(drift_ppm),
            anchors=anchor_tuple,
            reason=reason,
            created_ms=self._now(),
            created_by=actor.actor_id,
        )
        self.store.corrections[correction.correction_id] = correction
        self._journal(actor, "clock-correction", "correction", correction.correction_id, correction)
        return correction

    def add_segment(
        self, actor: Actor, stream_id: str, label: str, correction_id: str,
        start_device_ms: int, end_device_ms: int,
    ) -> Segment:
        """登记数据分段。派生视图：引用原始流与时钟校正。"""
        self._require(actor, Role.ANALYST)
        if stream_id not in self.store.streams:
            raise NotFound(f"原始流不存在: {stream_id}")
        correction = self.store.corrections.get(correction_id)
        if correction is None:
            raise NotFound(f"时钟校正不存在: {correction_id}")
        if correction.stream_id != stream_id:
            raise ValidationError("时钟校正不属于该原始流")
        if start_device_ms > end_device_ms:
            raise ValidationError("分段窗口不合法")
        segment = Segment(
            segment_id=self._gen_id("seg"),
            stream_id=stream_id,
            label=label,
            correction_id=correction_id,
            start_device_ms=start_device_ms,
            end_device_ms=end_device_ms,
            created_ms=self._now(),
        )
        self.store.segments[segment.segment_id] = segment
        self._journal(actor, "segment", "segment", segment.segment_id, segment)
        return segment

    def extract_segment(self, actor: Actor, segment_id: str) -> dict:
        """按分段规格取数：返回主时钟时间轴上的记录，原始流保持不变。"""
        self._require(actor, Role.ANALYST, Role.COORDINATOR)
        segment = self.store.segments.get(segment_id)
        if segment is None:
            raise NotFound(f"分段不存在: {segment_id}")
        stream = self.store.streams[segment.stream_id]
        correction = self.store.corrections[segment.correction_id]
        records = [
            [correction.to_master(t), payload]
            for t, payload in stream.records
            if segment.start_device_ms <= t <= segment.end_device_ms
        ]
        return {
            "segment_id": segment.segment_id,
            "stream_id": segment.stream_id,
            "label": segment.label,
            "correction_id": segment.correction_id,
            "records": records,
        }

    def align_events(
        self, actor: Actor, behavior_stream_id: str, neural_stream_id: str,
        behavior_correction_id: str, neural_correction_id: str,
    ):
        """登记事件对齐规格：行为事件经各自时钟校正对齐到脑信号时间轴。"""
        self._require(actor, Role.ANALYST)
        for stream_id in (behavior_stream_id, neural_stream_id):
            if stream_id not in self.store.streams:
                raise NotFound(f"原始流不存在: {stream_id}")
        for correction_id, stream_id in (
            (behavior_correction_id, behavior_stream_id),
            (neural_correction_id, neural_stream_id),
        ):
            correction = self.store.corrections.get(correction_id)
            if correction is None:
                raise NotFound(f"时钟校正不存在: {correction_id}")
            if correction.stream_id != stream_id:
                raise ValidationError("时钟校正与原始流不匹配")
        alignment = EventAlignment(
            alignment_id=self._gen_id("aln"),
            behavior_stream_id=behavior_stream_id,
            neural_stream_id=neural_stream_id,
            behavior_correction_id=behavior_correction_id,
            neural_correction_id=neural_correction_id,
            created_ms=self._now(),
        )
        self.store.alignments[alignment.alignment_id] = alignment
        self._journal(actor, "alignment", "alignment", alignment.alignment_id, alignment)
        return alignment, self._alignment_output(alignment)

    def _alignment_output(self, alignment: EventAlignment) -> dict:
        behavior = self.store.streams[alignment.behavior_stream_id]
        neural = self.store.streams[alignment.neural_stream_id]
        behavior_correction = self.store.corrections[alignment.behavior_correction_id]
        neural_correction = self.store.corrections[alignment.neural_correction_id]
        events = [
            [behavior_correction.to_master(t), payload] for t, payload in behavior.records
        ]
        return {
            "alignment_id": alignment.alignment_id,
            "neural_started_master_ms": neural_correction.to_master(neural.started_ms),
            "events": events,
        }

    def run_alignment(self, actor: Actor, alignment_id: str) -> dict:
        """按登记的对齐规格重算，结果确定，可复现。"""
        self._require(actor, Role.ANALYST, Role.COORDINATOR)
        alignment = self.store.alignments.get(alignment_id)
        if alignment is None:
            raise NotFound(f"事件对齐不存在: {alignment_id}")
        return self._alignment_output(alignment)

    # ------------------------------------------------------------------
    # 治理：预注册、分析、排除、发布
    # ------------------------------------------------------------------
    def register_hypothesis(
        self, actor: Actor, statement: str, data_range: DataRange,
        analysis_plan: str, code_version: str,
    ) -> Hypothesis:
        """预注册假设：注册后不可变，数据范围在此固定。"""
        self._require(actor, Role.ANALYST)
        for participant_id in data_range.participant_ids:
            self._participant(participant_id)
        hypothesis = Hypothesis(
            hypothesis_id=self._gen_id("hyp"),
            statement=statement,
            data_range=data_range,
            analysis_plan=analysis_plan,
            code_version=code_version,
            registered_ms=self._now(),
            registered_by=actor.actor_id,
        )
        self.store.hypotheses[hypothesis.hypothesis_id] = hypothesis
        self._journal(actor, "hypothesis", "hypothesis", hypothesis.hypothesis_id, hypothesis)
        return hypothesis

    def record_exclusion(
        self, actor: Actor, target_kind: str, target_id: str, reason: str, code: str
    ) -> ExclusionRecord:
        """登记排除记录（如噪声试次、超同意会话）。"""
        self._require(actor, Role.ANALYST)
        if target_kind not in ("session", "trial", "stream"):
            raise ValidationError("排除对象类型必须是 session/trial/stream")
        exclusion = ExclusionRecord(
            exclusion_id=self._gen_id("exc"),
            target_kind=target_kind,
            target_id=target_id,
            reason=reason,
            code=code,
            created_ms=self._now(),
            created_by=actor.actor_id,
        )
        self.store.exclusions[exclusion.exclusion_id] = exclusion
        self._journal(actor, "exclusion", "exclusion", exclusion.exclusion_id, exclusion)
        return exclusion

    def _sessions_in_range(self, data_range: DataRange) -> list:
        result = []
        for session in self.store.sessions.values():
            if (
                session.participant_id in data_range.participant_ids
                and session.status == SessionStatus.COMPLETED
                and session.stimulus_version in data_range.stimulus_versions
                and data_range.session_window[0] <= session.start_ms
                and session.end_ms <= data_range.session_window[1]
            ):
                result.append(session)
        return result

    def _collection_start(self, data_range: DataRange) -> Optional[int]:
        completions = [
            s.completed_ms for s in self._sessions_in_range(data_range) if s.completed_ms is not None
        ]
        return min(completions) if completions else None

    def run_analysis(
        self, actor: Actor, data_range: DataRange, code_version: str,
        electrode_versions, parameters: dict, result_summary: dict,
        exclusion_ids=(), alignment_ids=(), hypothesis_id: Optional[str] = None,
        supersedes: Optional[str] = None, zone: Optional[AnalysisZone] = None,
    ) -> Analysis:
        """登记一次分析版本。

        验证性分析必须引用预注册假设与固定数据范围，且假设注册先于数据采集；
        不满足这些条件的临时结果只能留在探索区。
        """
        self._require(actor, Role.ANALYST)
        for participant_id in data_range.participant_ids:
            self._participant(participant_id)
        for exclusion_id in exclusion_ids:
            if exclusion_id not in self.store.exclusions:
                raise NotFound(f"排除记录不存在: {exclusion_id}")
        for alignment_id in alignment_ids:
            if alignment_id not in self.store.alignments:
                raise NotFound(f"事件对齐不存在: {alignment_id}")
        for ref in electrode_versions:
            participant_id, _, version_text = str(ref).partition("#v")
            try:
                version = int(version_text)
            except ValueError as exc:
                raise ValidationError(f"电极版本引用非法: {ref}") from exc
            if version not in self.store.electrodes.get(participant_id, {}):
                raise NotFound(f"电极版本不存在: {ref}")

        sessions = self._sessions_in_range(data_range)
        quarantined = [
            stream_id
            for session in sessions
            for stream_id in session.stream_ids
            if stream_id in self.store.quarantined_streams
        ]
        if quarantined:
            raise ConsentViolation(
                f"数据已按同意决定隔离，不能进入分析: {sorted(quarantined)}"
            )
        collection_start = self._collection_start(data_range)

        requested = zone
        if requested is None:
            requested = AnalysisZone.CONFIRMATORY if hypothesis_id else AnalysisZone.EXPLORATORY
        if requested == AnalysisZone.CONFIRMATORY:
            if not hypothesis_id:
                raise GovernanceError("验证性分析必须引用预注册假设")
            hypothesis = self.store.hypotheses.get(hypothesis_id)
            if hypothesis is None:
                raise NotFound(f"假设不存在: {hypothesis_id}")
            if hypothesis.data_range != data_range:
                raise GovernanceError("分析数据范围必须与预注册时固定的数据范围一致")
            if collection_start is not None and hypothesis.registered_ms > collection_start:
                raise GovernanceError("假设注册晚于数据采集，不能作为验证性分析，只能留在探索区")

        if supersedes is not None:
            previous = self.store.analyses.get(supersedes)
            if previous is None:
                raise NotFound(f"被替代的分析不存在: {supersedes}")
            lineage = previous.lineage
        elif hypothesis_id:
            lineage = hypothesis_id
        else:
            lineage = "adhoc:" + content_hash(
                {"range": to_jsonable(data_range), "code": code_version}
            )[:16]
        version = 1 + max(
            (a.version for a in self.store.analyses.values() if a.lineage == lineage),
            default=0,
        )
        analysis = Analysis(
            analysis_id=self._gen_id("ana"),
            lineage=lineage,
            version=version,
            zone=requested,
            hypothesis_id=hypothesis_id,
            data_range=data_range,
            code_version=code_version,
            electrode_versions=tuple(electrode_versions),
            parameters=dict(parameters),
            result_summary=dict(result_summary),
            exclusion_ids=tuple(exclusion_ids),
            alignment_ids=tuple(alignment_ids),
            created_ms=self._now(),
            created_by=actor.actor_id,
            supersedes=supersedes,
        )
        self.store.analyses[analysis.analysis_id] = analysis
        self._journal(actor, "analysis", "analysis", analysis.analysis_id, analysis)
        return analysis

    def build_publication(self, actor: Actor, analysis_ids, conclusions) -> PublicationBundle:
        """为“做/不做”区域结论构建发布包，锁定复现所需的全部引用。"""
        self._require(actor, Role.ANALYST)
        if not analysis_ids:
            raise ValidationError("发布包至少引用一个分析")
        analyses = []
        for analysis_id in analysis_ids:
            analysis = self.store.analyses.get(analysis_id)
            if analysis is None:
                raise NotFound(f"分析不存在: {analysis_id}")
            if analysis.zone != AnalysisZone.CONFIRMATORY:
                raise GovernanceError(f"探索区结果不能作为发布依据: {analysis_id}")
            analyses.append(analysis)
        if not conclusions:
            raise ValidationError("发布包必须给出做/不做结论")
        parsed_conclusions = []
        for conclusion in conclusions:
            region = conclusion.get("region")
            recommendation = conclusion.get("recommendation")
            if not region or recommendation not in ("go", "no_go"):
                raise ValidationError("结论必须包含 region 与 go/no_go 推荐")
            parsed_conclusions.append(
                {"region": region, "recommendation": recommendation,
                 "rationale": conclusion.get("rationale", "")}
            )

        lineages = {a.lineage for a in analyses}
        hypothesis_ids = sorted({a.hypothesis_id for a in analyses})
        for hypothesis_id in hypothesis_ids:
            hypothesis = self.store.hypotheses[hypothesis_id]
            collection_start = self._collection_start(hypothesis.data_range)
            if collection_start is not None and hypothesis.registered_ms > collection_start:
                raise GovernanceError(f"假设 {hypothesis_id} 注册晚于数据采集，不能发布")
        exclusion_ids = sorted({e for a in analyses for e in a.exclusion_ids})
        alignment_ids = sorted({x for a in analyses for x in a.alignment_ids})
        version_ids = sorted(
            a.analysis_id for a in self.store.analyses.values() if a.lineage in lineages
        )
        stream_ids = sorted(
            {
                self.store.alignments[aid].behavior_stream_id
                for aid in alignment_ids
            }
            | {
                self.store.alignments[aid].neural_stream_id
                for aid in alignment_ids
            }
        )

        def ref(kind, entity_id, state_hash):
            return EntityRef(kind=kind, entity_id=entity_id, state_hash=state_hash)

        bundle = PublicationBundle(
            bundle_id=self._gen_id("pub"),
            analysis_refs=tuple(
                ref("analysis", a.analysis_id, hash_of(a)) for a in analyses
            ),
            hypothesis_refs=tuple(
                ref("hypothesis", hid, hash_of(self.store.hypotheses[hid]))
                for hid in hypothesis_ids
            ),
            exclusion_refs=tuple(
                ref("exclusion", eid, hash_of(self.store.exclusions[eid]))
                for eid in exclusion_ids
            ),
            alignment_refs=tuple(
                ref(
                    "alignment",
                    aid,
                    content_hash(
                        {
                            "spec": to_jsonable(self.store.alignments[aid]),
                            "output": self._alignment_output(self.store.alignments[aid]),
                        }
                    ),
                )
                for aid in alignment_ids
            ),
            version_refs=tuple(
                ref("analysis", aid, hash_of(self.store.analyses[aid])) for aid in version_ids
            ),
            stream_refs=tuple(
                ref("stream", sid, self.store.streams[sid].content_hash) for sid in stream_ids
            ),
            conclusions=tuple(parsed_conclusions),
            created_ms=self._now(),
            created_by=actor.actor_id,
        )
        self.store.publications[bundle.bundle_id] = bundle
        self._journal(actor, "publication", "publication", bundle.bundle_id, bundle)
        return bundle

    def verify_publication(self, actor: Actor, bundle_id: str) -> dict:
        """复现发布包：排除记录、事件对齐、多重分析版本与原始流逐项核对。"""
        self._require(actor, Role.ANALYST)
        bundle = self.store.publications.get(bundle_id)
        if bundle is None:
            raise NotFound(f"发布包不存在: {bundle_id}")
        checks = []

        def check(name, ok, detail=""):
            checks.append({"check": name, "ok": bool(ok), "detail": detail})

        stores = {
            "analysis": self.store.analyses,
            "hypothesis": self.store.hypotheses,
            "exclusion": self.store.exclusions,
            "stream": self.store.streams,
        }
        refs = (
            list(bundle.analysis_refs)
            + list(bundle.hypothesis_refs)
            + list(bundle.exclusion_refs)
            + list(bundle.version_refs)
            + list(bundle.stream_refs)
        )
        bad = []
        for entity_ref in refs:
            target = stores.get(entity_ref.kind, {}).get(entity_ref.entity_id)
            if target is None or hash_of(target) != entity_ref.state_hash:
                bad.append(f"{entity_ref.kind}:{entity_ref.entity_id}")
        check("引用对象完整且未被改动", not bad, ",".join(bad))

        zones_ok = all(
            self.store.analyses[r.entity_id].zone == AnalysisZone.CONFIRMATORY
            for r in bundle.analysis_refs
            if r.entity_id in self.store.analyses
        )
        check("仅包含验证区分析", zones_ok and bool(bundle.analysis_refs))

        late = []
        for hypothesis_ref in bundle.hypothesis_refs:
            hypothesis = self.store.hypotheses.get(hypothesis_ref.entity_id)
            if hypothesis is None:
                late.append(hypothesis_ref.entity_id)
                continue
            collection_start = self._collection_start(hypothesis.data_range)
            if collection_start is not None and hypothesis.registered_ms > collection_start:
                late.append(hypothesis.hypothesis_id)
        check("假设先于数据采集注册", not late, ",".join(late))

        missing_exclusions = [
            r.entity_id for r in bundle.exclusion_refs if r.entity_id not in self.store.exclusions
        ]
        check("排除记录可复现", not missing_exclusions, ",".join(missing_exclusions))

        bad_alignments = []
        for alignment_ref in bundle.alignment_refs:
            alignment = self.store.alignments.get(alignment_ref.entity_id)
            if alignment is None:
                bad_alignments.append(alignment_ref.entity_id)
                continue
            recomputed = content_hash(
                {"spec": to_jsonable(alignment), "output": self._alignment_output(alignment)}
            )
            if recomputed != alignment_ref.state_hash:
                bad_alignments.append(alignment_ref.entity_id)
        check("事件对齐可复现", not bad_alignments, ",".join(bad_alignments))

        expected_versions = set()
        for analysis_ref in bundle.analysis_refs:
            analysis = self.store.analyses.get(analysis_ref.entity_id)
            if analysis is None:
                continue
            expected_versions |= {
                a.analysis_id
                for a in self.store.analyses.values()
                if a.lineage == analysis.lineage
            }
        actual_versions = {r.entity_id for r in bundle.version_refs}
        check("多重分析版本齐全", expected_versions == actual_versions)

        ok = all(item["ok"] for item in checks)
        return {"bundle_id": bundle.bundle_id, "ok": ok, "checks": checks}

    # ------------------------------------------------------------------
    # 安全联动：不良反应、医嘱变化、参与者暂停
    # ------------------------------------------------------------------
    def record_adverse_event(
        self, actor: Actor, participant_id: str, severity, description: str, occurred_ms: int
    ):
        """不良反应：未开始环节立即失效，进行中的环节立即中止。"""
        self._require(actor, Role.CLINICIAN)
        self._participant(participant_id)
        try:
            level = severity if isinstance(severity, Severity) else Severity(severity)
        except ValueError as exc:
            raise ValidationError(f"不良事件级别非法: {exc}") from exc
        event = SafetyEvent(
            event_id=self._gen_id("aev"),
            participant_id=participant_id,
            kind="adverse_event",
            severity=level.value,
            description=description,
            occurred_ms=occurred_ms,
            recorded_ms=self._now(),
            recorded_by=actor.actor_id,
        )
        self.store.safety_events[event.event_id] = event
        self._journal(actor, "adverse-event", "safety_event", event.event_id, event)
        affected = self._suspend_sessions(actor, participant_id, f"adverse-event:{event.event_id}")
        return event, affected

    def record_order_change(
        self, actor: Actor, participant_id: str, order_id: str, note: str,
        effective_ms: int, suspend_research_until_ms: Optional[int] = None, blocks=(),
    ):
        """医嘱变化：可携带新的临床占时与暂停研究窗口，未开始环节立即重估。"""
        self._require(actor, Role.CLINICIAN)
        self._participant(participant_id)
        event = SafetyEvent(
            event_id=self._gen_id("ord"),
            participant_id=participant_id,
            kind="order_change",
            severity=None,
            description=note,
            occurred_ms=effective_ms,
            recorded_ms=self._now(),
            recorded_by=actor.actor_id,
            order_id=order_id,
            suspend_research_until_ms=suspend_research_until_ms,
        )
        self.store.safety_events[event.event_id] = event
        self._journal(actor, "order-change", "safety_event", event.event_id, event)
        invalidated = []
        for block in blocks:
            _, session_ids = self.add_clinical_block(
                actor,
                participant_id,
                block["kind"],
                block["start_ms"],
                block["end_ms"],
                order_id=order_id,
                note=block.get("note", ""),
            )
            invalidated.extend(session_ids)
        if suspend_research_until_ms is not None:
            invalidated.extend(
                self._invalidate_conflicting(
                    actor, participant_id, effective_ms, suspend_research_until_ms,
                    f"order-change:{event.event_id}",
                )
            )
        return event, invalidated

    def pause_participant(self, actor: Actor, participant_id: str, reason: str):
        """参与者暂停：未开始环节立即失效，已完成数据按同意决定处理。"""
        self._require(actor, Role.CLINICIAN, Role.COORDINATOR)
        participant = self._participant(participant_id)
        if participant.status != ParticipantStatus.ACTIVE:
            raise StateError(f"参与者状态为 {participant.status.value}，不能暂停")
        event = SafetyEvent(
            event_id=self._gen_id("pau"),
            participant_id=participant_id,
            kind="pause",
            severity=None,
            description=reason,
            occurred_ms=self._now(),
            recorded_ms=self._now(),
            recorded_by=actor.actor_id,
        )
        self.store.safety_events[event.event_id] = event
        self._journal(actor, "pause", "safety_event", event.event_id, event)
        participant.status = ParticipantStatus.PAUSED
        self._journal(actor, "pause", "participant", participant_id, participant)
        invalidated = self._suspend_sessions(actor, participant_id, f"pause:{event.event_id}")

        quarantined = []
        consent = self.current_consent(participant_id, self._now())
        if consent and consent.completed_data_policy == CompletedDataPolicy.DESTROY:
            for stream in self.store.streams.values():
                if stream.participant_id == participant_id:
                    self.store.quarantined_streams.add(stream.stream_id)
                    self._journal(
                        actor, "quarantine", "quarantine", stream.stream_id,
                        {"stream_id": stream.stream_id},
                    )
                    quarantined.append(stream.stream_id)
        return event, invalidated, sorted(quarantined)

    def resume_participant(self, actor: Actor, participant_id: str) -> Participant:
        """恢复参与。已失效环节不自动恢复，需重新排程；隔离数据不自动解除。"""
        self._require(actor, Role.CLINICIAN, Role.COORDINATOR)
        participant = self._participant(participant_id)
        if participant.status != ParticipantStatus.PAUSED:
            raise StateError(f"参与者状态为 {participant.status.value}，不能恢复")
        participant.status = ParticipantStatus.ACTIVE
        self._journal(actor, "resume", "participant", participant_id, participant)
        return participant

    def _suspend_sessions(self, actor: Actor, participant_id: str, reason: str) -> list:
        """未开始环节立即失效；进行中的环节立即中止。"""
        affected = []
        for session in self.store.sessions.values():
            if session.participant_id != participant_id:
                continue
            if session.status == SessionStatus.PLANNED:
                self._invalidate_session(actor, session, reason)
                affected.append(session.session_id)
            elif session.status == SessionStatus.IN_PROGRESS:
                self._abort_session(actor, session, reason)
                affected.append(session.session_id)
        return affected

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    def verify_journal(self, actor: Actor) -> list:
        """把只增日志折叠后与当前状态比对，返回不一致项。"""
        self._require(actor, Role.ANALYST, Role.COORDINATOR)
        return verify_journal(self.store)
