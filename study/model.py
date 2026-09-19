"""颅内决策研究协调的领域模型。

时间一律使用毫秒整数。不可变记录使用冻结 dataclass；
会随状态机演进的实体（参与者、环节）使用可变 dataclass，
每次变更都会写入只增日志。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Role(str, Enum):
    COORDINATOR = "coordinator"  # 研究协调员
    CLINICIAN = "clinician"      # 临床人员
    ANALYST = "analyst"          # 分析人员


@dataclass(frozen=True)
class Actor:
    """一次操作的执行者。"""

    role: Role
    actor_id: str


class ParticipantStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    WITHDRAWN = "withdrawn"


class ConsentScope(str, Enum):
    BEHAVIOR = "behavior"        # 行为数据
    NEURAL = "neural"            # 脑信号数据
    STORAGE = "storage"          # 数据留存
    PUBLICATION = "publication"  # 结论发表


class CompletedDataPolicy(str, Enum):
    """暂停或退出时，已完成数据按同意决定如何处理。"""

    RETAIN = "retain"    # 保留已完成数据
    DESTROY = "destroy"  # 已完成数据须隔离销毁


class BlockKind(str, Enum):
    """临床占时类型，一律临床优先。"""

    MONITORING = "monitoring"  # 临床监测
    REST = "rest"              # 医嘱休息
    PROCEDURE = "procedure"    # 临床处置
    IMAGING = "imaging"        # 影像检查


class SessionStatus(str, Enum):
    PLANNED = "planned"          # 已排程未开始
    IN_PROGRESS = "in_progress"  # 进行中
    COMPLETED = "completed"      # 已完成
    ABORTED = "aborted"          # 开始后因安全原因中止
    INVALIDATED = "invalidated"  # 未开始即失效


class Severity(str, Enum):
    MILD = "mild"
    MODERATE = "moderate"
    SEVERE = "severe"


class AnalysisZone(str, Enum):
    CONFIRMATORY = "confirmatory"  # 验证区：必须引用预注册假设与固定数据范围
    EXPLORATORY = "exploratory"    # 探索区：临时试出的结果只能留在这里


#: 采集一个实验环节所需的最小同意范围
REQUIRED_SESSION_SCOPES = frozenset({ConsentScope.BEHAVIOR, ConsentScope.NEURAL})


@dataclass(frozen=True)
class DataRange:
    """分析引用的固定数据范围（预注册时锁定）。"""

    participant_ids: tuple
    session_window: tuple  # (start_ms, end_ms)
    stimulus_versions: tuple

    @staticmethod
    def of(participant_ids, session_window, stimulus_versions) -> "DataRange":
        window = tuple(session_window)
        if len(window) != 2 or window[0] >= window[1]:
            raise ValueError("数据范围窗口不合法")
        return DataRange(
            tuple(sorted(participant_ids)),
            (int(window[0]), int(window[1])),
            tuple(sorted(stimulus_versions)),
        )


@dataclass(frozen=True)
class IdentityRecord:
    """直接身份信息，与分析视图隔离存放。"""

    participant_id: str
    legal_name: str
    medical_record_number: str


@dataclass
class Participant:
    participant_id: str
    inpatient_start_ms: int
    inpatient_end_ms: int
    status: ParticipantStatus = ParticipantStatus.ACTIVE


@dataclass(frozen=True)
class ConsentRecord:
    """同意记录，按版本追加，永不改写。"""

    participant_id: str
    version: int
    scopes: frozenset
    effective_from_ms: int
    effective_to_ms: Optional[int]
    completed_data_policy: CompletedDataPolicy
    recorded_ms: int
    recorded_by: str


@dataclass(frozen=True)
class Contact:
    contact_id: str
    region: str
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class ElectrodeLocalization:
    """电极位置版本（如术后 CT 重新配准产生新版本）。"""

    participant_id: str
    version: int
    contacts: tuple
    method: str
    effective_ms: int


@dataclass(frozen=True)
class TrialType:
    name: str
    gem_probability: float
    bomb_probability: float


@dataclass(frozen=True)
class StimulusSet:
    """刺激材料版本：宝石与炸弹概率及毫秒级时序参数，版本内不可变。"""

    version: str
    trial_types: tuple
    timing: dict
    content_hash: str


@dataclass(frozen=True)
class ClinicalBlock:
    """临床占时（监测/休息/处置/影像），临床优先。"""

    block_id: str
    participant_id: str
    kind: BlockKind
    start_ms: int
    end_ms: int
    order_id: Optional[str]
    note: str
    recorded_ms: int
    recorded_by: str


@dataclass
class Session:
    """一个实验环节。排程时钉住刺激、电极与同意版本。"""

    session_id: str
    participant_id: str
    start_ms: int
    end_ms: int
    stimulus_version: str
    electrode_version: int
    consent_version: int
    status: SessionStatus = SessionStatus.PLANNED
    invalidation: Optional[str] = None
    started_ms: Optional[int] = None
    completed_ms: Optional[int] = None
    stream_ids: list = field(default_factory=list)


@dataclass(frozen=True)
class RawStream:
    """原始流：设备时钟下的毫秒级行为事件或脑信号，摄取后不可变。"""

    stream_id: str
    session_id: str
    participant_id: str
    device: str
    clock_id: str
    started_ms: int
    records: tuple  # ((t_device_ms, payload), ...)
    content_hash: str


@dataclass(frozen=True)
class ClockCorrection:
    """设备时钟校正：派生层，引用原始流但绝不改动它。"""

    correction_id: str
    stream_id: str
    offset_ms: float
    drift_ppm: float
    anchors: tuple  # ((device_ms, master_ms), ...)
    reason: str
    created_ms: int
    created_by: str

    def to_master(self, t_device_ms: float) -> float:
        return t_device_ms * (1.0 + self.drift_ppm / 1_000_000.0) + self.offset_ms


@dataclass(frozen=True)
class Segment:
    """数据分段：派生视图，引用原始流与时钟校正。"""

    segment_id: str
    stream_id: str
    label: str
    correction_id: str
    start_device_ms: int
    end_device_ms: int
    created_ms: int


@dataclass(frozen=True)
class EventAlignment:
    """事件对齐规格：行为事件流对齐到脑信号流所用的校正版本。"""

    alignment_id: str
    behavior_stream_id: str
    neural_stream_id: str
    behavior_correction_id: str
    neural_correction_id: str
    created_ms: int


@dataclass(frozen=True)
class ExclusionRecord:
    """排除记录：分析可复现的前提之一。"""

    exclusion_id: str
    target_kind: str  # session | trial | stream
    target_id: str
    reason: str
    code: str
    created_ms: int
    created_by: str


@dataclass(frozen=True)
class Hypothesis:
    """预注册假设：注册后不可变，数据范围在此固定。"""

    hypothesis_id: str
    statement: str
    data_range: DataRange
    analysis_plan: str
    code_version: str
    registered_ms: int
    registered_by: str


@dataclass(frozen=True)
class Analysis:
    """一次分析版本。同一血缘（lineage）下的多重版本全部保留。"""

    analysis_id: str
    lineage: str
    version: int
    zone: AnalysisZone
    hypothesis_id: Optional[str]
    data_range: DataRange
    code_version: str
    electrode_versions: tuple
    parameters: dict
    result_summary: dict
    exclusion_ids: tuple
    alignment_ids: tuple
    created_ms: int
    created_by: str
    supersedes: Optional[str]


@dataclass(frozen=True)
class SafetyEvent:
    """安全事件：不良反应 / 医嘱变化 / 参与者暂停。"""

    event_id: str
    participant_id: str
    kind: str  # adverse_event | order_change | pause
    severity: Optional[str]
    description: str
    occurred_ms: int
    recorded_ms: int
    recorded_by: str
    order_id: Optional[str] = None
    suspend_research_until_ms: Optional[int] = None


@dataclass(frozen=True)
class EntityRef:
    """发布包中对某一对象某一状态的引用（内容哈希）。"""

    kind: str
    entity_id: str
    state_hash: str


@dataclass(frozen=True)
class PublicationBundle:
    """“做/不做”区域结论的发布包：须能复现排除记录、事件对齐与多重分析版本。"""

    bundle_id: str
    analysis_refs: tuple
    hypothesis_refs: tuple
    exclusion_refs: tuple
    alignment_refs: tuple
    version_refs: tuple
    stream_refs: tuple
    conclusions: tuple  # {"region": ..., "recommendation": "go"|"no_go"}
    created_ms: int
    created_by: str
