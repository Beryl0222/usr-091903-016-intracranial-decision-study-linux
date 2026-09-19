"""领域核心测试：逐条覆盖研究协调的不变量。"""

import unittest
from dataclasses import replace

from study.access import analysis_view, participant_view, safety_view
from study.errors import (
    AccessDenied,
    ClinicalConflict,
    ConsentViolation,
    GovernanceError,
    ImmutableViolation,
    StateError,
    ValidationError,
)
from study.model import (
    Actor,
    AnalysisZone,
    DataRange,
    ParticipantStatus,
    Role,
    SessionStatus,
)
from study.store import hash_of, verify_journal
from study.system import StudySystem

T0 = 1_700_000_000_000
HOUR = 3_600_000

COORD = Actor(Role.COORDINATOR, "coord-1")
CLIN = Actor(Role.CLINICIAN, "dr-1")
ANAL = Actor(Role.ANALYST, "ana-1")

FULL_SCOPES = {"behavior", "neural", "storage", "publication"}


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def make_system(policy="retain", scopes=FULL_SCOPES):
    clock = Clock()
    system = StudySystem(now_ms=clock)
    system.enroll_participant(
        COORD, "P01",
        {"legal_name": "患者甲", "medical_record_number": "MRN-001"},
        T0, T0 + 72 * HOUR,
    )
    system.record_consent(COORD, "P01", scopes, T0, T0 + 72 * HOUR, policy)
    system.record_electrodes(
        COORD, "P01",
        [{"contact_id": "A1", "region": "OFC", "x": 1.0, "y": 2.0, "z": 3.0}],
        "术后CT配准", T0,
    )
    system.register_stimulus_set(
        COORD, "stim-1.0",
        [{"name": "std", "gem_probability": 0.7, "bomb_probability": 0.3}],
        {"cue_ms": 500, "decision_ms": 1500, "iti_ms": 800},
    )
    return system, clock


def run_session(system, clock, start_ms, end_ms, stimulus="stim-1.0", pid="P01"):
    """排程、开始、摄取双路原始流并完成一个环节。"""
    session = system.schedule_session(COORD, pid, start_ms, end_ms, stimulus)
    clock.t = start_ms
    system.start_session(COORD, session.session_id)
    behavior = system.ingest_stream(
        COORD, session.session_id, "behavior-rig", "clk-beh", start_ms,
        [[0, {"event": "cue"}], [1500, {"event": "choice", "gem": True}]],
    )
    neural = system.ingest_stream(
        COORD, session.session_id, "neural-amp", "clk-neu", start_ms,
        [[0, {"ch": "A1", "v": 0.1}], [1, {"ch": "A1", "v": 0.2}]],
    )
    clock.t = end_ms
    system.complete_session(COORD, session.session_id)
    return session, behavior, neural


def data_range(window=(T0, T0 + 72 * HOUR)):
    return DataRange.of(["P01"], window, ["stim-1.0"])


class SchedulingTest(unittest.TestCase):
    def test_schedule_in_free_gap(self):
        system, _ = make_system()
        system.add_clinical_block(CLIN, "P01", "monitoring", T0 + 3 * HOUR, T0 + 4 * HOUR)
        session = system.schedule_session(COORD, "P01", T0 + 4 * HOUR, T0 + 5 * HOUR, "stim-1.0")
        self.assertEqual(session.status, SessionStatus.PLANNED)
        self.assertEqual(session.stimulus_version, "stim-1.0")
        self.assertEqual(session.electrode_version, 1)
        self.assertEqual(session.consent_version, 1)

    def test_session_overlapping_clinical_block_rejected(self):
        system, _ = make_system()
        system.add_clinical_block(CLIN, "P01", "rest", T0 + 3 * HOUR, T0 + 4 * HOUR)
        with self.assertRaises(ClinicalConflict):
            system.schedule_session(COORD, "P01", T0 + 3 * HOUR + 1, T0 + 5 * HOUR, "stim-1.0")

    def test_new_clinical_block_invalidates_planned_session(self):
        """临床优先：新临床占时出现时，冲突的未开始环节立即失效。"""
        system, _ = make_system()
        session = system.schedule_session(COORD, "P01", T0 + 5 * HOUR, T0 + 6 * HOUR, "stim-1.0")
        _, invalidated = system.add_clinical_block(
            CLIN, "P01", "monitoring", T0 + 5 * HOUR + 1, T0 + 7 * HOUR
        )
        self.assertEqual(invalidated, [session.session_id])
        self.assertEqual(session.status, SessionStatus.INVALIDATED)
        self.assertTrue(session.invalidation.startswith("clinical-priority:"))

    def test_session_outside_inpatient_window_rejected(self):
        system, _ = make_system()
        with self.assertRaises(ValidationError):
            system.schedule_session(COORD, "P01", T0 + 80 * HOUR, T0 + 81 * HOUR, "stim-1.0")

    def test_schedule_requires_consent_scope(self):
        system, _ = make_system(scopes={"behavior"})
        with self.assertRaises(ConsentViolation):
            system.schedule_session(COORD, "P01", T0 + 1 * HOUR, T0 + 2 * HOUR, "stim-1.0")

    def test_schedule_requires_electrode_version(self):
        clock = Clock()
        system = StudySystem(now_ms=clock)
        system.enroll_participant(
            COORD, "P02", {"legal_name": "患者乙", "medical_record_number": "MRN-002"},
            T0, T0 + 72 * HOUR,
        )
        system.record_consent(COORD, "P02", FULL_SCOPES, T0, T0 + 72 * HOUR, "retain")
        system.register_stimulus_set(
            COORD, "stim-1.0",
            [{"name": "std", "gem_probability": 0.5, "bomb_probability": 0.5}],
            {"cue_ms": 500},
        )
        with self.assertRaises(ValidationError):
            system.schedule_session(COORD, "P02", T0 + 1 * HOUR, T0 + 2 * HOUR, "stim-1.0")

    def test_overlapping_sessions_rejected(self):
        system, _ = make_system()
        system.schedule_session(COORD, "P01", T0 + 1 * HOUR, T0 + 2 * HOUR, "stim-1.0")
        with self.assertRaises(ValidationError):
            system.schedule_session(COORD, "P01", T0 + 90 * 60 * 1000, T0 + 3 * HOUR, "stim-1.0")

    def test_stimulus_probabilities_validated_and_immutable(self):
        system, _ = make_system()
        with self.assertRaises(ValidationError):
            system.register_stimulus_set(
                COORD, "stim-bad",
                [{"name": "x", "gem_probability": 1.2, "bomb_probability": 0.1}],
                {"cue_ms": 500},
            )
        with self.assertRaises(ImmutableViolation):
            system.register_stimulus_set(
                COORD, "stim-1.0",
                [{"name": "std", "gem_probability": 0.6, "bomb_probability": 0.4}],
                {"cue_ms": 500},
            )

    def test_session_state_machine(self):
        system, clock = make_system()
        session = system.schedule_session(COORD, "P01", T0 + 1 * HOUR, T0 + 2 * HOUR, "stim-1.0")
        with self.assertRaises(StateError):
            system.complete_session(COORD, session.session_id)
        clock.t = T0 + 1 * HOUR
        system.start_session(COORD, session.session_id)
        with self.assertRaises(StateError):
            system.start_session(COORD, session.session_id)


class StreamImmutabilityTest(unittest.TestCase):
    def test_raw_stream_frozen_and_hash_stable(self):
        """时钟校正与数据分段不得改动原始流。"""
        system, clock = make_system()
        _, behavior, neural = run_session(system, clock, T0 + 1 * HOUR, T0 + 2 * HOUR)
        behavior_hash = behavior.content_hash
        neural_hash = neural.content_hash

        correction = system.add_clock_correction(
            ANAL, behavior.stream_id, 2.0, 0.0, [[0, 2]], "设备时钟校正"
        )
        segment = system.add_segment(
            ANAL, neural.stream_id, "决策窗", self._neural_correction(system, neural), 0, 1
        )
        system.extract_segment(ANAL, segment.segment_id)

        untouched_behavior = system.store.streams[behavior.stream_id]
        untouched_neural = system.store.streams[neural.stream_id]
        self.assertEqual(untouched_behavior.content_hash, behavior_hash)
        self.assertEqual(untouched_neural.content_hash, neural_hash)
        self.assertEqual(hash_of(untouched_behavior), behavior_hash)
        with self.assertRaises(Exception):
            untouched_behavior.records = ()  # 冻结对象不允许改写
        self.assertEqual(correction.stream_id, behavior.stream_id)

    def _neural_correction(self, system, neural):
        return system.add_clock_correction(
            ANAL, neural.stream_id, -5.0, 0.0, [[0, -5]], "设备时钟校正"
        ).correction_id

    def test_clock_correction_maps_time(self):
        system, clock = make_system()
        _, behavior, _ = run_session(system, clock, T0 + 1 * HOUR, T0 + 2 * HOUR)
        correction = system.add_clock_correction(
            ANAL, behavior.stream_id, offset_ms=2.0, drift_ppm=0.0,
            anchors=[[0, 2]], reason="校正",
        )
        self.assertEqual(correction.to_master(1500), 1502.0)
        drifted = system.add_clock_correction(
            ANAL, behavior.stream_id, offset_ms=0.0, drift_ppm=1_000_000.0,
            anchors=[[0, 0]], reason="漂移",
        )
        self.assertEqual(drifted.to_master(1500), 3000.0)

    def test_segment_extraction_uses_corrected_time(self):
        system, clock = make_system()
        _, _, neural = run_session(system, clock, T0 + 1 * HOUR, T0 + 2 * HOUR)
        correction = system.add_clock_correction(
            ANAL, neural.stream_id, 100.0, 0.0, [[0, 100]], "校正"
        )
        segment = system.add_segment(ANAL, neural.stream_id, "前1ms", correction.correction_id, 0, 0)
        extracted = system.extract_segment(ANAL, segment.segment_id)
        self.assertEqual(extracted["records"], [[100.0, {"ch": "A1", "v": 0.1}]])
        # 原始流记录仍为设备时钟
        self.assertEqual(system.store.streams[neural.stream_id].records[0][0], 0)

    def test_alignment_is_deterministic(self):
        system, clock = make_system()
        _, behavior, neural = run_session(system, clock, T0 + 1 * HOUR, T0 + 2 * HOUR)
        beh_corr = system.add_clock_correction(ANAL, behavior.stream_id, 2.0, 0.0, [[0, 2]], "c")
        neu_corr = system.add_clock_correction(ANAL, neural.stream_id, -5.0, 0.0, [[0, -5]], "c")
        alignment, output = system.align_events(
            ANAL, behavior.stream_id, neural.stream_id,
            beh_corr.correction_id, neu_corr.correction_id,
        )
        replayed = system.run_alignment(ANAL, alignment.alignment_id)
        self.assertEqual(output, replayed)
        self.assertEqual(output["events"][0][0], 2.0)
        self.assertEqual(output["neural_started_master_ms"], (T0 + 1 * HOUR) - 5.0)

    def test_ingest_requires_monotonic_records(self):
        system, clock = make_system()
        session = system.schedule_session(COORD, "P01", T0 + 1 * HOUR, T0 + 2 * HOUR, "stim-1.0")
        clock.t = T0 + 1 * HOUR
        system.start_session(COORD, session.session_id)
        with self.assertRaises(ValidationError):
            system.ingest_stream(
                COORD, session.session_id, "dev", "clk", T0 + 1 * HOUR,
                [[5, "a"], [3, "b"]],
            )


class GovernanceTest(unittest.TestCase):
    def _collected_system(self):
        system, clock = make_system()
        system.register_hypothesis(
            ANAL, "OFC 活动预测风险选择", data_range(), "logistic 回归", "v1"
        )
        session, behavior, neural = run_session(system, clock, T0 + 1 * HOUR, T0 + 2 * HOUR)
        return system, clock, session, behavior, neural

    def test_confirmatory_analysis_ok_when_preregistered(self):
        system, clock, session, behavior, neural = self._collected_system()
        hypothesis = next(iter(system.store.hypotheses.values()))
        exclusion = system.record_exclusion(ANAL, "trial", "trial-7", "肌电伪迹", "ARTIFACT")
        beh_corr = system.add_clock_correction(ANAL, behavior.stream_id, 2.0, 0.0, [[0, 2]], "c")
        neu_corr = system.add_clock_correction(ANAL, neural.stream_id, -5.0, 0.0, [[0, -5]], "c")
        alignment, _ = system.align_events(
            ANAL, behavior.stream_id, neural.stream_id,
            beh_corr.correction_id, neu_corr.correction_id,
        )
        clock.t = T0 + 3 * HOUR
        analysis = system.run_analysis(
            ANAL, data_range(), "v1", ["P01#v1"], {"model": "logistic"},
            {"effect": 0.42},
            exclusion_ids=[exclusion.exclusion_id],
            alignment_ids=[alignment.alignment_id],
            hypothesis_id=hypothesis.hypothesis_id,
        )
        self.assertEqual(analysis.zone, AnalysisZone.CONFIRMATORY)
        self.assertEqual(analysis.version, 1)

    def test_confirmatory_requires_hypothesis(self):
        system, clock, *_ = self._collected_system()
        with self.assertRaises(GovernanceError):
            system.run_analysis(
                ANAL, data_range(), "v1", ["P01#v1"], {}, {},
                zone=AnalysisZone.CONFIRMATORY,
            )

    def test_confirmatory_requires_fixed_data_range(self):
        system, clock, *_ = self._collected_system()
        hypothesis = next(iter(system.store.hypotheses.values()))
        narrower = DataRange.of(["P01"], (T0, T0 + 90 * 60 * 1000), ["stim-1.0"])
        with self.assertRaises(GovernanceError):
            system.run_analysis(
                ANAL, narrower, "v1", ["P01#v1"], {}, {},
                hypothesis_id=hypothesis.hypothesis_id,
            )

    def test_late_hypothesis_cannot_be_confirmatory(self):
        """先采集后注册的假设只能进探索区。"""
        system, clock = make_system()
        run_session(system, clock, T0 + 1 * HOUR, T0 + 2 * HOUR)
        clock.t = T0 + 3 * HOUR
        hypothesis = system.register_hypothesis(ANAL, "事后假设", data_range(), "plan", "v1")
        with self.assertRaises(GovernanceError):
            system.run_analysis(
                ANAL, data_range(), "v1", ["P01#v1"], {}, {},
                hypothesis_id=hypothesis.hypothesis_id,
            )

    def test_adhoc_result_stays_exploratory(self):
        system, clock, *_ = self._collected_system()
        analysis = system.run_analysis(ANAL, data_range(), "scratch", ["P01#v1"], {}, {"peek": 1})
        self.assertEqual(analysis.zone, AnalysisZone.EXPLORATORY)
        with self.assertRaises(GovernanceError):
            system.build_publication(
                ANAL, [analysis.analysis_id],
                [{"region": "OFC", "recommendation": "go"}],
            )

    def test_analysis_versions_kept(self):
        """同一血缘的多重分析版本全部保留并进入发布包。"""
        system, clock, *_ = self._collected_system()
        hypothesis = next(iter(system.store.hypotheses.values()))
        clock.t = T0 + 3 * HOUR
        v1 = system.run_analysis(
            ANAL, data_range(), "v1", ["P01#v1"], {}, {"effect": 0.4},
            hypothesis_id=hypothesis.hypothesis_id,
        )
        v2 = system.run_analysis(
            ANAL, data_range(), "v1.1", ["P01#v1"], {}, {"effect": 0.42},
            hypothesis_id=hypothesis.hypothesis_id, supersedes=v1.analysis_id,
        )
        self.assertEqual(v2.version, 2)
        bundle = system.build_publication(
            ANAL, [v2.analysis_id], [{"region": "OFC", "recommendation": "go"}]
        )
        version_ids = {r.entity_id for r in bundle.version_refs}
        self.assertEqual(version_ids, {v1.analysis_id, v2.analysis_id})

    def test_publication_verify_reproduces_bundle(self):
        system, clock, session, behavior, neural = self._collected_system()
        hypothesis = next(iter(system.store.hypotheses.values()))
        exclusion = system.record_exclusion(ANAL, "trial", "trial-7", "伪迹", "ARTIFACT")
        beh_corr = system.add_clock_correction(ANAL, behavior.stream_id, 2.0, 0.0, [[0, 2]], "c")
        neu_corr = system.add_clock_correction(ANAL, neural.stream_id, -5.0, 0.0, [[0, -5]], "c")
        alignment, _ = system.align_events(
            ANAL, behavior.stream_id, neural.stream_id,
            beh_corr.correction_id, neu_corr.correction_id,
        )
        clock.t = T0 + 3 * HOUR
        analysis = system.run_analysis(
            ANAL, data_range(), "v1", ["P01#v1"], {}, {"effect": 0.42},
            exclusion_ids=[exclusion.exclusion_id],
            alignment_ids=[alignment.alignment_id],
            hypothesis_id=hypothesis.hypothesis_id,
        )
        bundle = system.build_publication(
            ANAL, [analysis.analysis_id],
            [{"region": "OFC", "recommendation": "go"},
             {"region": "amygdala", "recommendation": "no_go"}],
        )
        report = system.verify_publication(ANAL, bundle.bundle_id)
        self.assertTrue(report["ok"], report)
        self.assertEqual(len(report["checks"]), 6)

    def test_publication_verify_detects_tampering(self):
        system, clock, *_ = self._collected_system()
        hypothesis = next(iter(system.store.hypotheses.values()))
        exclusion = system.record_exclusion(ANAL, "trial", "trial-7", "伪迹", "ARTIFACT")
        clock.t = T0 + 3 * HOUR
        analysis = system.run_analysis(
            ANAL, data_range(), "v1", ["P01#v1"], {}, {"effect": 0.42},
            exclusion_ids=[exclusion.exclusion_id],
            hypothesis_id=hypothesis.hypothesis_id,
        )
        bundle = system.build_publication(
            ANAL, [analysis.analysis_id], [{"region": "OFC", "recommendation": "go"}]
        )
        tampered = replace(system.store.exclusions[exclusion.exclusion_id], reason="被改写")
        system.store.exclusions[exclusion.exclusion_id] = tampered
        report = system.verify_publication(ANAL, bundle.bundle_id)
        self.assertFalse(report["ok"])
        failed = {c["check"] for c in report["checks"] if not c["ok"]}
        self.assertIn("引用对象完整且未被改动", failed)


class SafetyLinkageTest(unittest.TestCase):
    def test_adverse_event_invalidates_unstarted_and_aborts_running(self):
        system, clock = make_system()
        planned = system.schedule_session(COORD, "P01", T0 + 5 * HOUR, T0 + 6 * HOUR, "stim-1.0")
        running = system.schedule_session(COORD, "P01", T0 + 1 * HOUR, T0 + 2 * HOUR, "stim-1.0")
        clock.t = T0 + 1 * HOUR
        system.start_session(COORD, running.session_id)
        event, affected = system.record_adverse_event(
            CLIN, "P01", "severe", "癫痫样放电", T0 + 1 * HOUR
        )
        self.assertEqual(set(affected), {planned.session_id, running.session_id})
        self.assertEqual(planned.status, SessionStatus.INVALIDATED)
        self.assertEqual(running.status, SessionStatus.ABORTED)
        self.assertTrue(planned.invalidation.startswith(f"adverse-event:{event.event_id}"))

    def test_order_change_suspend_window_invalidates_sessions(self):
        system, clock = make_system()
        session = system.schedule_session(COORD, "P01", T0 + 10 * HOUR, T0 + 11 * HOUR, "stim-1.0")
        _, invalidated = system.record_order_change(
            CLIN, "P01", "ORD-1", "术后观察", T0,
            suspend_research_until_ms=T0 + 12 * HOUR,
        )
        self.assertEqual(invalidated, [session.session_id])
        self.assertEqual(session.status, SessionStatus.INVALIDATED)

    def test_order_change_with_new_block_invalidates(self):
        system, clock = make_system()
        session = system.schedule_session(COORD, "P01", T0 + 8 * HOUR, T0 + 9 * HOUR, "stim-1.0")
        _, invalidated = system.record_order_change(
            CLIN, "P01", "ORD-2", "加做监测", T0,
            blocks=[{"kind": "monitoring", "start_ms": T0 + 8 * HOUR, "end_ms": T0 + 10 * HOUR}],
        )
        self.assertEqual(invalidated, [session.session_id])

    def test_pause_retains_completed_data_when_policy_retain(self):
        system, clock = make_system(policy="retain")
        session, behavior, _ = run_session(system, clock, T0 + 1 * HOUR, T0 + 2 * HOUR)
        planned = system.schedule_session(COORD, "P01", T0 + 5 * HOUR, T0 + 6 * HOUR, "stim-1.0")
        _, invalidated, quarantined = system.pause_participant(CLIN, "P01", "疲劳")
        self.assertEqual(invalidated, [planned.session_id])
        self.assertEqual(quarantined, [])
        self.assertEqual(system.store.participants["P01"].status, ParticipantStatus.PAUSED)
        with self.assertRaises(StateError):
            system.schedule_session(COORD, "P01", T0 + 7 * HOUR, T0 + 8 * HOUR, "stim-1.0")
        system.resume_participant(CLIN, "P01")
        again = system.schedule_session(COORD, "P01", T0 + 7 * HOUR, T0 + 8 * HOUR, "stim-1.0")
        self.assertEqual(again.status, SessionStatus.PLANNED)

    def test_pause_with_destroy_policy_quarantines_completed_data(self):
        """已完成数据按同意决定处理：destroy 政策下隔离并禁止进入分析。"""
        system, clock = make_system(policy="destroy")
        session, behavior, neural = run_session(system, clock, T0 + 1 * HOUR, T0 + 2 * HOUR)
        system.register_hypothesis(ANAL, "H", data_range(), "plan", "v1")
        _, _, quarantined = system.pause_participant(CLIN, "P01", "撤回")
        self.assertEqual(sorted(quarantined), sorted([behavior.stream_id, neural.stream_id]))
        hypothesis = next(iter(system.store.hypotheses.values()))
        with self.assertRaises(ConsentViolation):
            system.run_analysis(
                ANAL, data_range(), "v1", ["P01#v1"], {}, {},
                hypothesis_id=hypothesis.hypothesis_id,
            )


class AccessControlTest(unittest.TestCase):
    def test_analyst_view_hides_direct_identity(self):
        system, clock = make_system()
        run_session(system, clock, T0 + 1 * HOUR, T0 + 2 * HOUR)
        view = participant_view(system, ANAL, "P01")
        self.assertNotIn("identity", view)
        self.assertNotIn("患者甲", str(view))
        self.assertEqual(len(view["sessions"]), 1)
        self.assertEqual(view["sessions"][0]["stimulus_version"], "stim-1.0")

    def test_clinician_sees_safety_without_research_inference(self):
        system, clock = make_system()
        run_session(system, clock, T0 + 1 * HOUR, T0 + 2 * HOUR)
        system.record_adverse_event(CLIN, "P01", "mild", "头痛", T0 + 3 * HOUR)
        view = safety_view(system, CLIN, "P01")
        self.assertEqual(view["identity"]["legal_name"], "患者甲")
        self.assertEqual(len(view["safety_events"]), 1)
        self.assertNotIn("analyses", view)
        self.assertNotIn("stimulus_version", str(view["sessions"]))
        with self.assertRaises(AccessDenied):
            safety_view(system, ANAL, "P01")

    def test_clinician_cannot_read_analysis(self):
        system, clock = make_system()
        system.register_hypothesis(ANAL, "H", data_range(), "plan", "v1")
        run_session(system, clock, T0 + 1 * HOUR, T0 + 2 * HOUR)
        hypothesis = next(iter(system.store.hypotheses.values()))
        clock.t = T0 + 3 * HOUR
        analysis = system.run_analysis(
            ANAL, data_range(), "v1", ["P01#v1"], {}, {"effect": 0.4},
            hypothesis_id=hypothesis.hypothesis_id,
        )
        self.assertEqual(analysis_view(system, ANAL, analysis.analysis_id)["zone"], "confirmatory")
        with self.assertRaises(AccessDenied):
            analysis_view(system, CLIN, analysis.analysis_id)

    def test_role_enforcement_on_mutations(self):
        system, clock = make_system()
        with self.assertRaises(AccessDenied):
            system.add_clinical_block(ANAL, "P01", "rest", T0, T0 + HOUR)
        with self.assertRaises(AccessDenied):
            system.schedule_session(CLIN, "P01", T0 + HOUR, T0 + 2 * HOUR, "stim-1.0")
        with self.assertRaises(AccessDenied):
            system.record_adverse_event(COORD, "P01", "mild", "x", T0)
        with self.assertRaises(AccessDenied):
            system.run_analysis(CLIN, data_range(), "v1", ["P01#v1"], {}, {})


class JournalTest(unittest.TestCase):
    def test_journal_replays_to_live_state(self):
        system, clock = make_system()
        system.register_hypothesis(ANAL, "H", data_range(), "plan", "v1")
        session, behavior, neural = run_session(system, clock, T0 + 1 * HOUR, T0 + 2 * HOUR)
        correction = system.add_clock_correction(ANAL, behavior.stream_id, 2.0, 0.0, [[0, 2]], "c")
        system.add_segment(ANAL, behavior.stream_id, "s", correction.correction_id, 0, 1500)
        system.record_exclusion(ANAL, "trial", "t-3", "伪迹", "ARTIFACT")
        system.record_adverse_event(CLIN, "P01", "mild", "头痛", T0 + 3 * HOUR)
        self.assertEqual(verify_journal(system.store), [])
        self.assertEqual(system.verify_journal(ANAL), [])


if __name__ == "__main__":
    unittest.main()
