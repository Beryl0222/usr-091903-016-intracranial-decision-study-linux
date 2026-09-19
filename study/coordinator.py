"""研究协调核心逻辑：临床优先、同意驱动、分析可复现。

设计要点
--------
* 同意范围（consent scope）决定数据可见性与处置；撤回/暂停只影响未开始环节，
  已完成数据按“撤回处置策略”（retain 保留 / deidentify 去标识保留 / destroy 销毁）处理。
* 安全事件（不良反应 / 医嘱变化 / 参与者暂停）立即使未开始环节失效，
  进行中的环节被中断；未解除前不允许排程或开始任何新环节。
* 原始流不可变；时钟校正（clock alignment）与数据分段（segmentation）
  都产生独立的、版本化的派生对象，并回链原始流哈希。
* 每个分析必须引用预注册假设与固定数据范围；确认性结论冻结范围、排除记录与
  分析版本，探索性结果只能停留在 exploratory 区，不能发布。
* 角色隔离：clinical 只看安全信息，analyst 只看研究代码与去标识数据，
  coordinator 负责编排。
"""

import copy
import threading

from study.store import JsonStore, RawStreamStore, AuditLog, utc_now


class DomainError(Exception):
    """业务规则冲突。"""

    def __init__(self, message, code="rule_violation"):
        super().__init__(message)
        self.code = code


# 固定词汇表 ---------------------------------------------------------------

CONSENT_SCOPES = (
    "behavior",          # 行为数据
    "neural",            # 脑信号
    "stimuli",           # 刺激材料反应记录
    "future_contact",    # 后续联系
)

WITHDRAW_DISPOSITIONS = ("retain", "deidentify", "destroy")

STOP_REASONS = ("adverse_event", "medical_order_change", "participant_pause")

EVENT_LIFECYCLE = ("raised", "cleared")
SESSION_LIFECYCLE = (
    "scheduled",      # 已排程，未开始
    "running",        # 进行中
    "completed",      # 已完成
    "interrupted",    # 进行中被安全事件中断
    "invalidated",    # 未开始即被安全事件/同意变更作废
)

ANALYSIS_ZONES = ("confirmatory", "exploratory")
PUBLICATION_STATES = ("draft", "released")


class Coordinator:
    """无状态外观 + 可持久化存储，便于单元测试直接驱动。"""

    def __init__(self, data_dir):
        self.store = JsonStore(f"{data_dir}/state.json")
        self.raw = RawStreamStore(f"{data_dir}/raw")
        self.audit = AuditLog(f"{data_dir}/audit.log")
        # 粗粒度协调锁：HTTP 层在整个“校验 + 变更 + 落账”期间持有，
        # 保证并发请求不会交错写 state.json。
        self.lock = threading.RLock()
        self._seed_tokens()

    # ---- 内部工具 ---------------------------------------------------------

    def _seed_tokens(self):
        tokens = self.store.collection("tokens")
        for token, role in (
            ("coord-token", "coordinator"),
            ("clinical-token", "clinical"),
            ("analyst-token", "analyst"),
        ):
            tokens.setdefault(token, {"role": role})

    def require_role(self, token, *roles):
        tokens = self.store.collection("tokens")
        record = tokens.get(token)
        if record is None:
            raise DomainError("未认证的访问令牌", "unauthorized")
        if roles and record["role"] not in roles:
            raise DomainError(
                f"角色 {record['role']} 无权执行该操作", "forbidden"
            )
        return record["role"]

    def _get(self, collection, key, kind):
        record = self.store.collection(collection).get(key)
        if record is None:
            raise DomainError(f"{kind}不存在：{key}", "not_found")
        return record

    @staticmethod
    def _public_view(record, *, redact_identity=False):
        view = copy.deepcopy(record)
        if redact_identity and "identity" in view:
            view.pop("identity", None)
        return view

    def _save_audit(self, action, actor, details):
        self.store.save()
        return self.audit.append(action, actor, details)

    # ---- 参与者与同意 -----------------------------------------------------

    def enroll(self, token, code, identity=None, consent_scopes=None,
               withdraw_disposition="retain"):
        """登记参与者。code 为研究代码（分析师可见），identity 为直接身份（仅协调/临床可见）。"""
        role = self.require_role(token, "coordinator")
        participants = self.store.collection("participants")
        if code in participants:
            raise DomainError(f"研究代码已存在：{code}", "conflict")
        scopes = self._validate_scopes(consent_scopes or [])
        if withdraw_disposition not in WITHDRAW_DISPOSITIONS:
            raise DomainError(
                f"撤回处置必须是 {WITHDRAW_DISPOSITIONS} 之一", "invalid_input"
            )
        record = {
            "code": code,
            "identity": identity or {},
            "consent_scopes": sorted(scopes),
            "withdraw_disposition": withdraw_disposition,
            "enrolled_at": utc_now(),
            "active": True,
            "consent_version": 1,
            "consent_history": [
                {
                    "version": 1,
                    "at": utc_now(),
                    "scopes": sorted(scopes),
                    "change": "initial",
                }
            ],
        }
        participants[code] = record
        self._save_audit("participant.enroll", role, {"code": code})
        return self._public_view(record)

    @staticmethod
    def _validate_scopes(scopes):
        scopes = list(scopes)
        unknown = sorted(set(scopes) - set(CONSENT_SCOPES))
        if unknown:
            raise DomainError(f"未知同意范围：{unknown}", "invalid_input")
        if len(scopes) != len(set(scopes)):
            raise DomainError("同意范围存在重复", "invalid_input")
        return scopes

    def update_consent(self, token, code, consent_scopes, reason="amendment"):
        """更新同意范围（新版本）。范围收窄会立即作废依赖被收窄范围的未开始环节。"""
        role = self.require_role(token, "coordinator")
        participant = self._get("participants", code, "参与者")
        new_scopes = set(self._validate_scopes(consent_scopes))
        old_scopes = set(participant["consent_scopes"])
        removed = old_scopes - new_scopes
        participant["consent_scopes"] = sorted(new_scopes)
        participant["consent_version"] += 1
        participant["consent_history"].append(
            {
                "version": participant["consent_version"],
                "at": utc_now(),
                "scopes": sorted(new_scopes),
                "change": reason,
                "removed": sorted(removed),
            }
        )
        affected = []
        if removed:
            affected = self._invalidate_sessions(
                code,
                reason=f"consent_narrowed:{','.join(sorted(removed))}",
                scope_filter=removed,
            )
        self._save_audit(
            "participant.consent_update",
            role,
            {"code": code, "removed": sorted(removed), "invalidated": affected},
        )
        return self._public_view(participant), affected

    def withdraw(self, token, code, reason="participant_withdrawal"):
        """撤回同意：未开始环节立即失效，已完成数据按预设处置策略处理。"""
        role = self.require_role(token, "coordinator")
        participant = self._get("participants", code, "参与者")
        disposition = participant["withdraw_disposition"]
        participant["active"] = False
        participant["withdrawn_at"] = utc_now()
        invalidated = self._invalidate_sessions(code, reason=reason)
        interrupted = self._interrupt_running_sessions(code, reason=reason)
        disposition_result = self._apply_withdrawal_disposition(code, disposition)
        self._save_audit(
            "participant.withdraw",
            role,
            {
                "code": code,
                "disposition": disposition,
                "invalidated": invalidated,
                "interrupted": interrupted,
                **disposition_result,
            },
        )
        return {
            "code": code,
            "disposition": disposition,
            "invalidated_sessions": invalidated,
            "interrupted_sessions": interrupted,
            **disposition_result,
        }

    def _apply_withdrawal_disposition(self, code, disposition):
        """对已完成数据执行撤回处置。原始流字节不删；destroy 只在索引层标记停用。"""
        sessions = self.store.collection("sessions")
        completed = [
            sid for sid, s in sessions.items()
            if s["participant_code"] == code and s["status"] == "completed"
        ]
        affected_streams = []
        if disposition == "retain":
            return {"completed_sessions": completed, "streams_revoked": []}
        if disposition == "deidentify":
            participant = self.store.collection("participants")[code]
            participant["identity"] = {}
            participant["deidentified"] = True
        elif disposition == "destroy":
            streams = self.store.collection("streams")
            for stream in streams.values():
                if stream["participant_code"] == code and stream["status"] == "active":
                    stream["status"] = "destroyed_by_consent"
                    stream["destroyed_at"] = utc_now()
                    affected_streams.append(stream["stream_id"])
        return {
            "completed_sessions": completed,
            "streams_revoked": affected_streams,
        }

    # ---- 电极位置版本 -----------------------------------------------------

    def register_electrode_layout(self, token, participant_code, layout_id, contacts,
                                  implanted_at, notes=""):
        """登记电极布局版本。contacts 为 [{contact, region, hemisphere, x, y, z}, ...]。"""
        role = self.require_role(token, "coordinator")
        self._get("participants", participant_code, "参与者")
        layouts = self.store.collection("layouts")
        if layout_id in layouts:
            raise DomainError(f"电极布局版本已存在：{layout_id}", "conflict")
        if not contacts:
            raise DomainError("电极布局至少包含一个触点", "invalid_input")
        for contact in contacts:
            for required in ("contact", "region"):
                if required not in contact:
                    raise DomainError(f"触点缺少字段：{required}", "invalid_input")
        record = {
            "layout_id": layout_id,
            "participant_code": participant_code,
            "contacts": contacts,
            "implanted_at": implanted_at,
            "registered_at": utc_now(),
            "notes": notes,
            "superseded_by": None,
        }
        layouts[layout_id] = record
        self._save_audit("electrode.layout_register", role, {"layout_id": layout_id})
        return record

    def supersede_layout(self, token, old_layout_id, new_layout_id):
        role = self.require_role(token, "coordinator")
        old = self._get("layouts", old_layout_id, "电极布局")
        old["superseded_by"] = new_layout_id
        self._save_audit(
            "electrode.layout_supersede",
            role,
            {"old": old_layout_id, "new": new_layout_id},
        )
        return old

    # ---- 刺激材料与概率配置 ----------------------------------------------

    def register_stimulus_set(self, token, stimulus_set_id, items, version=1):
        """登记刺激材料集（图片/任务文本等），内容整体版本化。"""
        role = self.require_role(token, "coordinator")
        sets = self.store.collection("stimulus_sets")
        if stimulus_set_id in sets:
            raise DomainError(f"刺激材料集已存在：{stimulus_set_id}", "conflict")
        if not items:
            raise DomainError("刺激材料集不能为空", "invalid_input")
        record = {
            "stimulus_set_id": stimulus_set_id,
            "version": version,
            "items": items,
            "registered_at": utc_now(),
        }
        sets[stimulus_set_id] = record
        self._save_audit("stimulus.set_register", role, {"stimulus_set_id": stimulus_set_id})
        return record

    def register_game_config(self, token, config_id, gem_probability, bomb_probability,
                             *, n_choices=2, description=""):
        """登记风险游戏配置：宝石/炸弹概率在使用前固定，概率和必须为 1。"""
        role = self.require_role(token, "coordinator")
        configs = self.store.collection("game_configs")
        if config_id in configs:
            raise DomainError(f"游戏配置已存在：{config_id}", "conflict")
        self._validate_probabilities(gem_probability, bomb_probability)
        record = {
            "config_id": config_id,
            "gem_probability": gem_probability,
            "bomb_probability": bomb_probability,
            "n_choices": n_choices,
            "description": description,
            "registered_at": utc_now(),
        }
        configs[config_id] = record
        self._save_audit("game.config_register", role, {"config_id": config_id})
        return record

    @staticmethod
    def _validate_probabilities(gem, bomb):
        if not isinstance(gem, (int, float)) or not isinstance(bomb, (int, float)):
            raise DomainError("概率必须为数值", "invalid_input")
        if not (0 <= gem <= 1) or not (0 <= bomb <= 1):
            raise DomainError("概率必须在 [0,1] 区间", "invalid_input")
        if abs((gem + bomb) - 1.0) > 1e-9:
            raise DomainError(
                f"宝石概率与炸弹概率之和必须为 1（当前 {gem + bomb}）", "invalid_input"
            )

    # ---- 排程与临床优先 ---------------------------------------------------

    def active_stop_event(self, participant_code):
        events = self.store.collection("safety_events")
        for event in events.values():
            if (event["participant_code"] == participant_code
                    and event["status"] == "raised"):
                return event
        return None

    def schedule_session(self, token, session_id, participant_code, planned_start,
                         stimulus_set_id, config_id, layout_id, required_scopes):
        """排程环节。存在未解除安全事件、参与者不活跃或同意范围不足时拒绝排程。"""
        role = self.require_role(token, "coordinator")
        participant = self._get("participants", participant_code, "参与者")
        sessions = self.store.collection("sessions")
        if session_id in sessions:
            raise DomainError(f"环节已存在：{session_id}", "conflict")
        self._guard_clinical_readiness(participant)
        self._require_scopes(participant, required_scopes)
        for collection, key, kind in (
            ("stimulus_sets", stimulus_set_id, "刺激材料集"),
            ("game_configs", config_id, "游戏配置"),
            ("layouts", layout_id, "电极布局"),
        ):
            self._get(collection, key, kind)
        record = {
            "session_id": session_id,
            "participant_code": participant_code,
            "planned_start": planned_start,
            "stimulus_set_id": stimulus_set_id,
            "config_id": config_id,
            "layout_id": layout_id,
            "required_scopes": sorted(required_scopes),
            "status": "scheduled",
            "created_at": utc_now(),
            "invalidated_reason": None,
            "timeline": [],
        }
        sessions[session_id] = record
        self._save_audit("session.schedule", role, {"session_id": session_id})
        return record

    def start_session(self, token, session_id):
        role = self.require_role(token, "coordinator")
        session = self._get("sessions", session_id, "环节")
        if session["status"] != "scheduled":
            raise DomainError(
                f"环节当前状态 {session['status']}，不能开始", "invalid_state"
            )
        participant = self._get(
            "participants", session["participant_code"], "参与者"
        )
        self._guard_clinical_readiness(participant)
        self._require_scopes(participant, session["required_scopes"])
        session["status"] = "running"
        session["started_at"] = utc_now()
        session["timeline"].append(
            {"event": "started", "at": session["started_at"]}
        )
        self._save_audit("session.start", role, {"session_id": session_id})
        return session

    def complete_session(self, token, session_id):
        role = self.require_role(token, "coordinator")
        session = self._get("sessions", session_id, "环节")
        if session["status"] != "running":
            raise DomainError(
                f"环节当前状态 {session['status']}，不能完成", "invalid_state"
            )
        session["status"] = "completed"
        session["completed_at"] = utc_now()
        session["timeline"].append(
            {"event": "completed", "at": session["completed_at"]}
        )
        self._save_audit("session.complete", role, {"session_id": session_id})
        return session

    def _guard_clinical_readiness(self, participant):
        if not participant.get("active", True):
            raise DomainError("参与者同意已撤回，不能安排或开始环节", "consent_inactive")
        stop = self.active_stop_event(participant["code"])
        if stop is not None:
            raise DomainError(
                f"存在未解除的安全事件 {stop['event_id']}（{stop['reason']}），"
                "临床优先：禁止安排或开始环节",
                "clinical_hold",
            )

    @staticmethod
    def _require_scopes(participant, scopes):
        missing = sorted(set(scopes) - set(participant["consent_scopes"]))
        if missing:
            raise DomainError(f"同意范围不足，缺少：{missing}", "consent_scope")

    def raise_safety_event(self, token, event_id, participant_code, reason,
                           message, severity="moderate"):
        """上报安全事件：未开始环节立即失效，进行中环节立即中断。"""
        role = self.require_role(token, "coordinator", "clinical")
        if reason not in STOP_REASONS:
            raise DomainError(
                f"安全事件原因必须是 {STOP_REASONS} 之一", "invalid_input"
            )
        self._get("participants", participant_code, "参与者")
        events = self.store.collection("safety_events")
        if event_id in events:
            raise DomainError(f"安全事件已存在：{event_id}", "conflict")
        record = {
            "event_id": event_id,
            "participant_code": participant_code,
            "reason": reason,
            "message": message,
            "severity": severity,
            "status": "raised",
            "raised_at": utc_now(),
            "raised_by": role,
            "cleared_at": None,
        }
        events[event_id] = record
        invalidated = self._invalidate_sessions(participant_code, reason=reason)
        interrupted = self._interrupt_running_sessions(participant_code, reason=reason)
        self._save_audit(
            "safety.raise",
            role,
            {
                "event_id": event_id,
                "invalidated": invalidated,
                "interrupted": interrupted,
            },
        )
        return {
            "event": record,
            "invalidated_sessions": invalidated,
            "interrupted_sessions": interrupted,
        }

    def clear_safety_event(self, token, event_id, resolution_note=""):
        """临床确认解除后才允许恢复排程/开始。"""
        role = self.require_role(token, "coordinator", "clinical")
        event = self._get("safety_events", event_id, "安全事件")
        if event["status"] != "raised":
            raise DomainError("安全事件已解除", "invalid_state")
        event["status"] = "cleared"
        event["cleared_at"] = utc_now()
        event["resolution_note"] = resolution_note
        self._save_audit("safety.clear", role, {"event_id": event_id})
        return event

    def _invalidate_sessions(self, participant_code, *, reason, scope_filter=None):
        """作废未开始环节。scope_filter 给定时只作废依赖被收窄范围的环节。"""
        affected = []
        for session in self.store.collection("sessions").values():
            if session["participant_code"] != participant_code:
                continue
            if session["status"] != "scheduled":
                continue
            if scope_filter and not (
                set(session["required_scopes"]) & scope_filter
            ):
                continue
            session["status"] = "invalidated"
            session["invalidated_reason"] = reason
            session["invalidated_at"] = utc_now()
            session["timeline"].append(
                {"event": "invalidated", "at": session["invalidated_at"], "reason": reason}
            )
            affected.append(session["session_id"])
        return sorted(affected)

    def _interrupt_running_sessions(self, participant_code, *, reason):
        """中断进行中环节（数据保留，状态标记为 interrupted）。"""
        affected = []
        for session in self.store.collection("sessions").values():
            if session["participant_code"] != participant_code:
                continue
            if session["status"] != "running":
                continue
            session["status"] = "interrupted"
            session["interrupted_at"] = utc_now()
            session["interrupt_reason"] = reason
            session["timeline"].append(
                {"event": "interrupted", "at": session["interrupted_at"], "reason": reason}
            )
            affected.append(session["session_id"])
        return sorted(affected)

    def resume_interrupted_session(self, token, session_id):
        """安全事件解除后，中断的环节可恢复为 running；已作废环节不能复活。"""
        role = self.require_role(token, "coordinator")
        session = self._get("sessions", session_id, "环节")
        if session["status"] != "interrupted":
            raise DomainError(
                f"环节当前状态 {session['status']}，不能恢复", "invalid_state"
            )
        participant = self._get(
            "participants", session["participant_code"], "参与者"
        )
        self._guard_clinical_readiness(participant)
        session["status"] = "running"
        at = utc_now()
        session["timeline"].append({"event": "resumed", "at": at})
        self._save_audit("session.resume", role, {"session_id": session_id})
        return session

    # ---- 原始流、时钟校正与事件对齐 --------------------------------------

    def register_raw_stream(self, token, stream_id, session_id, scope, label,
                            payload=b""):
        """登记毫秒级行为/脑信号原始流。原始字节此后不可修改。"""
        role = self.require_role(token, "coordinator")
        session = self._get("sessions", session_id, "环节")
        if session["status"] != "running":
            raise DomainError(
                f"环节当前状态 {session['status']}：原始流只能在进行中的环节登记",
                "invalid_state",
            )
        if scope not in CONSENT_SCOPES:
            raise DomainError(f"数据类型必须是 {CONSENT_SCOPES} 之一", "invalid_input")
        participant = self._get(
            "participants", session["participant_code"], "参与者"
        )
        self._require_scopes(participant, [scope])
        streams = self.store.collection("streams")
        if stream_id in streams:
            raise DomainError(f"原始流已登记：{stream_id}", "conflict")
        manifest = self.raw.register(
            stream_id, label, utc_now(), payload or b""
        )
        record = {
            "stream_id": stream_id,
            "session_id": session_id,
            "participant_code": session["participant_code"],
            "scope": scope,
            "label": label,
            "status": "active",
            "registered_at": utc_now(),
            **manifest,
        }
        streams[stream_id] = record
        self._save_audit("stream.register", role, {"stream_id": stream_id})
        return record

    def append_raw_stream(self, token, stream_id, payload):
        """向原始流追加字节（例如设备续传），不允许修改已有内容。"""
        role = self.require_role(token, "coordinator")
        record = self._get("streams", stream_id, "原始流")
        session = self._get("sessions", record["session_id"], "环节")
        if session["status"] != "running":
            raise DomainError(
                f"环节当前状态 {session['status']}：只允许向进行中的环节追加原始流",
                "invalid_state",
            )
        if record["status"] != "active":
            raise DomainError(
                f"原始流状态 {record['status']}，不可写入", "invalid_state"
            )
        manifest = self.raw.append(
            stream_id, record["label"], utc_now(), payload
        )
        record.update(manifest)
        self._save_audit(
            "stream.append", role, {"stream_id": stream_id, "bytes": len(payload)}
        )
        return record

    def apply_clock_correction(self, token, alignment_id, stream_id, offset_ms,
                               anchor_event, reference_clock="study_master"):
        """登记时钟校正（派生视图）：仅记录偏移与锚点，绝不回写原始流。"""
        role = self.require_role(token, "coordinator")
        stream = self._get("streams", stream_id, "原始流")
        if not isinstance(offset_ms, (int, float)):
            raise DomainError("offset_ms 必须为数值", "invalid_input")
        alignments = self.store.collection("alignments")
        if alignment_id in alignments:
            raise DomainError(f"对齐版本已存在：{alignment_id}", "conflict")
        record = {
            "alignment_id": alignment_id,
            "stream_id": stream_id,
            "source_sha256": stream["sha256"],
            "source_bytes": stream["bytes"],
            "offset_ms": offset_ms,
            "anchor_event": anchor_event,
            "reference_clock": reference_clock,
            "created_at": utc_now(),
            "derived": True,
        }
        alignments[alignment_id] = record
        self._save_audit(
            "alignment.create",
            role,
            {"alignment_id": alignment_id, "stream_id": stream_id},
        )
        return record

    def create_segmentation(self, token, segmentation_id, stream_id, alignment_id,
                            segments):
        """基于对齐版本创建分段（派生数据）。segments 为 [{segment_id, start_ms, end_ms, label}]。"""
        role = self.require_role(token, "coordinator")
        stream = self._get("streams", stream_id, "原始流")
        alignment = self._get("alignments", alignment_id, "时钟对齐")
        if alignment["stream_id"] != stream_id:
            raise DomainError("对齐版本与原始流不匹配", "invalid_input")
        if not segments:
            raise DomainError("分段列表不能为空", "invalid_input")
        for seg in segments:
            for required in ("segment_id", "start_ms", "end_ms"):
                if required not in seg:
                    raise DomainError(f"分段缺少字段：{required}", "invalid_input")
            if seg["end_ms"] <= seg["start_ms"]:
                raise DomainError(
                    f"分段 {seg['segment_id']} 结束时间必须晚于开始时间",
                    "invalid_input",
                )
        tables = self.store.collection("segmentations")
        if segmentation_id in tables:
            raise DomainError(f"分段版本已存在：{segmentation_id}", "conflict")
        record = {
            "segmentation_id": segmentation_id,
            "stream_id": stream_id,
            "alignment_id": alignment_id,
            "source_sha256": stream["sha256"],
            "source_bytes": stream["bytes"],
            "segments": segments,
            "created_at": utc_now(),
            "derived": True,
        }
        tables[segmentation_id] = record
        self._save_audit(
            "segmentation.create",
            role,
            {"segmentation_id": segmentation_id, "n_segments": len(segments)},
        )
        return record

    # ---- 预注册、排除与分析 ----------------------------------------------

    def preregister_hypothesis(self, token, hypothesis_id, statement,
                               directional_prediction, planned_analysis,
                               fixed_session_ids, fixed_scopes):
        """预注册假设并固定数据范围（环节集合 + 数据类型）。"""
        role = self.require_role(token, "coordinator", "analyst")
        hypotheses = self.store.collection("hypotheses")
        if hypothesis_id in hypotheses:
            raise DomainError(f"假设已预注册：{hypothesis_id}", "conflict")
        sessions = self.store.collection("sessions")
        unknown = sorted(set(fixed_session_ids) - set(sessions))
        if unknown:
            raise DomainError(f"固定范围包含不存在的环节：{unknown}", "invalid_input")
        self._validate_scopes(fixed_scopes)
        record = {
            "hypothesis_id": hypothesis_id,
            "statement": statement,
            "directional_prediction": directional_prediction,
            "planned_analysis": planned_analysis,
            "fixed_session_ids": sorted(fixed_session_ids),
            "fixed_scopes": sorted(fixed_scopes),
            "registered_at": utc_now(),
            "registered_by": role,
        }
        hypotheses[hypothesis_id] = record
        self._save_audit("hypothesis.preregister", role, {"hypothesis_id": hypothesis_id})
        return record

    def create_exclusion_log(self, token, exclusion_id, hypothesis_id,
                             exclusions, rule_version):
        """登记可复现的排除记录：每条含环节、规则与原因，整体带规则版本。"""
        role = self.require_role(token, "coordinator", "analyst")
        self._get("hypotheses", hypothesis_id, "预注册假设")
        # 无排除时也必须显式登记空集（exclusions=[]），作为可复现产物。
        sessions = self.store.collection("sessions")
        for item in exclusions:
            for required in ("session_id", "rule", "reason"):
                if required not in item:
                    raise DomainError(f"排除条目缺少字段：{required}", "invalid_input")
            if item["session_id"] not in sessions:
                raise DomainError(
                    f"排除条目引用不存在的环节：{item['session_id']}", "invalid_input"
                )
        logs = self.store.collection("exclusion_logs")
        if exclusion_id in logs:
            raise DomainError(f"排除记录已存在：{exclusion_id}", "conflict")
        record = {
            "exclusion_id": exclusion_id,
            "hypothesis_id": hypothesis_id,
            "rule_version": rule_version,
            "exclusions": exclusions,
            "created_at": utc_now(),
        }
        logs[exclusion_id] = record
        self._save_audit("exclusion.create", role, {"exclusion_id": exclusion_id})
        return record

    def run_analysis(self, token, analysis_id, hypothesis_id, zone,
                     exclusion_id=None, alignment_ids=None, segmentation_ids=None,
                     result=None, analyst_note=""):
        """运行分析。confirmatory 必须引用预注册假设、固定范围、排除记录与对齐/分段版本；
        exploratory 结果只能留在探索区。"""
        role = self.require_role(token, "coordinator", "analyst")
        if zone not in ANALYSIS_ZONES:
            raise DomainError(f"分析区域必须是 {ANALYSIS_ZONES} 之一", "invalid_input")
        hypothesis = self._get("hypotheses", hypothesis_id, "预注册假设")
        sessions = self.store.collection("sessions")
        fixed_ids = set(hypothesis["fixed_session_ids"])
        missing_sessions = sorted(sid for sid in fixed_ids if sid not in sessions)
        if missing_sessions:
            raise DomainError(
                f"固定范围内环节已不存在：{missing_sessions}", "range_broken"
            )
        excluded_ids = set()
        if exclusion_id is not None:
            exclusion = self._get("exclusion_logs", exclusion_id, "排除记录")
            if exclusion["hypothesis_id"] != hypothesis_id:
                raise DomainError("排除记录与假设不匹配", "invalid_input")
            excluded_ids = {item["session_id"] for item in exclusion["exclusions"]}
            stray = sorted(excluded_ids - fixed_ids)
            if stray:
                raise DomainError(
                    f"排除记录引用了固定范围外环节：{stray}", "range_broken"
                )
        elif zone == "confirmatory":
            raise DomainError(
                "确认性分析必须引用排除记录（无排除也需显式登记空集）", "missing_exclusion"
            )

        included_session_ids = sorted(fixed_ids - excluded_ids)
        # 固定范围快照：记录当时每个环节的状态，供复现核对。
        range_snapshot = {
            sid: {"status": sessions[sid]["status"]} for sid in sorted(fixed_ids)
        }
        align_refs = self._freeze_derivatives(
            "alignments", alignment_ids or [], included_session_ids
        )
        seg_refs = self._freeze_derivatives(
            "segmentations", segmentation_ids or [], included_session_ids
        )
        analyses = self.store.collection("analyses")
        if analysis_id in analyses:
            raise DomainError(f"分析已存在：{analysis_id}", "conflict")
        record = {
            "analysis_id": analysis_id,
            "hypothesis_id": hypothesis_id,
            "zone": zone,
            "exclusion_id": exclusion_id,
            "included_session_ids": included_session_ids,
            "fixed_range_snapshot": range_snapshot,
            "alignment_refs": align_refs,
            "segmentation_refs": seg_refs,
            "result": result or {},
            "analyst_note": analyst_note,
            "created_at": utc_now(),
            "created_by": role,
            "publication_state": "draft",
        }
        analyses[analysis_id] = record
        self._save_audit(
            "analysis.run",
            role,
            {"analysis_id": analysis_id, "zone": zone},
        )
        return record

    def _freeze_derivatives(self, collection, ids, included_session_ids):
        """冻结对齐/分段版本引用，校验它们只来自范围内环节。"""
        streams = self.store.collection("streams")
        allowed_sessions = set(included_session_ids)
        refs = []
        for did in ids:
            record = self._get(collection, did, "派生版本")
            stream = streams.get(record["stream_id"])
            if stream is None or stream["session_id"] not in allowed_sessions:
                raise DomainError(
                    f"派生版本 {did} 的数据不在固定分析范围内", "range_broken"
                )
            refs.append(
                {
                    "id": did,
                    "stream_id": record["stream_id"],
                    "source_sha256": record["source_sha256"],
                    "source_bytes": record["source_bytes"],
                }
            )
        return refs

    def release_publication(self, token, publication_id, analysis_id,
                            decision_label):
        """发布“做/不做”结论。只有 confirmatory 分析可发布；发布时复现闸门必须通过。"""
        role = self.require_role(token, "coordinator", "analyst")
        analysis = self._get("analyses", analysis_id, "分析")
        if analysis["zone"] != "confirmatory":
            raise DomainError(
                "探索性分析结果不得作为结论发布，只能留在探索区", "exploratory_blocked"
            )
        self.verify_reproducibility(analysis_id)
        publications = self.store.collection("publications")
        if publication_id in publications:
            raise DomainError(f"发布记录已存在：{publication_id}", "conflict")
        record = {
            "publication_id": publication_id,
            "analysis_id": analysis_id,
            "hypothesis_id": analysis["hypothesis_id"],
            "decision_label": decision_label,
            "state": "released",
            "released_at": utc_now(),
            "released_by": role,
            "reproducibility": self.reproducibility_report(analysis_id),
        }
        publications[publication_id] = record
        analysis["publication_state"] = "released"
        self._save_audit(
            "publication.release",
            role,
            {"publication_id": publication_id, "analysis_id": analysis_id},
        )
        return record

    def reproducibility_report(self, analysis_id):
        """产出可复现性报告：排除记录、事件对齐、多重分析版本均可复核。"""
        analysis = self._get("analyses", analysis_id, "分析")
        sessions = self.store.collection("sessions")
        checks = []

        # 1) 固定范围未被破坏，环节状态与快照一致。
        for sid, snap in analysis["fixed_range_snapshot"].items():
            current = sessions[sid]["status"]
            checks.append(
                {
                    "check": "range_session_status",
                    "session_id": sid,
                    "expected": snap["status"],
                    "actual": current,
                    "passed": current == snap["status"],
                }
            )

        # 2) 排除记录仍可重放：included = fixed - excluded。
        if analysis["exclusion_id"]:
            exclusion = self.store.collection("exclusion_logs")[analysis["exclusion_id"]]
            replay_excluded = {
                item["session_id"] for item in exclusion["exclusions"]
            }
            replay_included = sorted(
                set(analysis["fixed_range_snapshot"]) - replay_excluded
            )
            checks.append(
                {
                    "check": "exclusion_replay",
                    "passed": replay_included == analysis["included_session_ids"],
                    "replayed_included": replay_included,
                }
            )

        # 3) 事件对齐：原始流已存在字节与派生版本锚定时一致（允许后续追加，禁止改写）。
        for ref in analysis["alignment_refs"]:
            actual, nread = self.raw.prefix_digest(
                ref["stream_id"], ref["source_bytes"]
            )
            checks.append(
                {
                    "check": "alignment_source_hash",
                    "stream_id": ref["stream_id"],
                    "expected": ref["source_sha256"],
                    "actual": actual,
                    "passed": actual == ref["source_sha256"]
                    and nread == ref["source_bytes"],
                }
            )
        for ref in analysis["segmentation_refs"]:
            actual, nread = self.raw.prefix_digest(
                ref["stream_id"], ref["source_bytes"]
            )
            checks.append(
                {
                    "check": "segmentation_source_hash",
                    "stream_id": ref["stream_id"],
                    "expected": ref["source_sha256"],
                    "actual": actual,
                    "passed": actual == ref["source_sha256"]
                    and nread == ref["source_bytes"],
                }
            )

        # 4) 同一假设的多重分析版本全部可枚举（防止挑选版本）。
        sibling_versions = sorted(
            aid for aid, a in self.store.collection("analyses").items()
            if a["hypothesis_id"] == analysis["hypothesis_id"]
        )
        checks.append(
            {
                "check": "analysis_versions_enumerable",
                "versions": sibling_versions,
                "passed": analysis_id in sibling_versions,
            }
        )

        # 5) 审计账完整。
        chain_ok, broken_at = self.audit.verify_chain()
        checks.append(
            {
                "check": "audit_chain",
                "passed": chain_ok,
                "broken_at": broken_at,
            }
        )
        return {
            "analysis_id": analysis_id,
            "all_passed": all(item["passed"] for item in checks),
            "checks": checks,
        }

    def verify_reproducibility(self, analysis_id):
        report = self.reproducibility_report(analysis_id)
        if not report["all_passed"]:
            failed = [c["check"] for c in report["checks"] if not c["passed"]]
            raise DomainError(
                f"可复现性闸门未通过：{failed}", "reproducibility_failed"
            )
        return report

    # ---- 角色化视图 -------------------------------------------------------

    def clinical_safety_view(self, token, participant_code=None):
        """临床视图：仅安全信息与环节状态，不含研究推断/结果。"""
        self.require_role(token, "coordinator", "clinical")
        events = self.store.collection("safety_events")
        sessions = self.store.collection("sessions")
        result = []
        for event in events.values():
            if participant_code and event["participant_code"] != participant_code:
                continue
            participant_sessions = [
                {
                    "session_id": s["session_id"],
                    "status": s["status"],
                    "planned_start": s["planned_start"],
                }
                for s in sessions.values()
                if s["participant_code"] == event["participant_code"]
            ]
            result.append(
                {
                    "event_id": event["event_id"],
                    "participant_code": event["participant_code"],
                    "reason": event["reason"],
                    "message": event["message"],
                    "severity": event["severity"],
                    "status": event["status"],
                    "raised_at": event["raised_at"],
                    "cleared_at": event["cleared_at"],
                    "sessions": participant_sessions,
                }
            )
        return {"safety_events": result}

    def analyst_view(self, token):
        """分析视图：研究代码、去标识参与者、配置、假设、分析；绝不出现直接身份。"""
        self.require_role(token, "coordinator", "analyst")
        participants = [
            self._public_view(p, redact_identity=True)
            for p in self.store.collection("participants").values()
        ]
        for view in participants:
            assert "identity" not in view
        return {
            "participants": participants,
            "layouts": list(self.store.collection("layouts").values()),
            "stimulus_sets": list(self.store.collection("stimulus_sets").values()),
            "game_configs": list(self.store.collection("game_configs").values()),
            "hypotheses": list(self.store.collection("hypotheses").values()),
            "exclusion_logs": list(self.store.collection("exclusion_logs").values()),
            "analyses": [
                self._analysis_for_analyst(a)
                for a in self.store.collection("analyses").values()
            ],
        }

    @staticmethod
    def _analysis_for_analyst(analysis):
        return {
            "analysis_id": analysis["analysis_id"],
            "hypothesis_id": analysis["hypothesis_id"],
            "zone": analysis["zone"],
            "included_session_ids": analysis["included_session_ids"],
            "publication_state": analysis["publication_state"],
            "result": analysis["result"],
            "analyst_note": analysis["analyst_note"],
        }

    def session_status_board(self, token):
        """协调视图：全部环节的生命周期与失效/中断原因。"""
        self.require_role(token, "coordinator")
        sessions = self.store.collection("sessions")
        return {
            "sessions": [
                {
                    "session_id": s["session_id"],
                    "participant_code": s["participant_code"],
                    "status": s["status"],
                    "planned_start": s["planned_start"],
                    "invalidated_reason": s.get("invalidated_reason"),
                    "interrupt_reason": s.get("interrupt_reason"),
                }
                for s in sessions.values()
            ]
        }
