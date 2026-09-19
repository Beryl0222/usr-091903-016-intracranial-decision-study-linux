"""研究协调领域契约：逐条锁定临床优先、同意驱动、不可变与可复现要求。"""

import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json

from service import build_handler
from study.coordinator import Coordinator, DomainError

COORD = "coord-token"
CLIN = "clinical-token"
ANALYST = "analyst-token"


class CoordinatorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.c = Coordinator(self.tmp)
        self._bootstrap()

    def _bootstrap(self, code="P01", scopes=("behavior", "neural", "stimuli")):
        self.c.enroll(
            COORD, code, identity={"name": "张三", "mrn": "MRN-1"},
            consent_scopes=list(scopes),
        )
        self.c.register_electrode_layout(
            COORD, code, "L1",
            [{"contact": "a1", "region": "amygdala", "hemisphere": "L"}],
            implanted_at=1000.0,
        )
        self.c.register_stimulus_set(COORD, "S1", [{"id": "img1", "kind": "cue"}])
        self.c.register_game_config(COORD, "G1", 0.6, 0.4)

    def _schedule(self, sid, code="P01", scopes=("behavior", "neural"), start=2000.0):
        return self.c.schedule_session(
            COORD, sid, code, start, "S1", "G1", "L1", list(scopes)
        )


class ConsentContractTests(CoordinatorCase):
    def test_consent_scopes_validated(self):
        with self.assertRaises(DomainError) as err:
            self.c.enroll(COORD, "PBAD", consent_scopes=["genome"])
        self.assertEqual(err.exception.code, "invalid_input")

    def test_scope_narrowing_invalidates_only_dependent_unstarted_sessions(self):
        self._schedule("SE-NEURAL", scopes=("neural",))
        self._schedule("SE-BEHAVIOR", scopes=("behavior",), start=3000.0)
        _, affected = self.c.update_consent(
            COORD, "P01", ["behavior", "stimuli"]
        )  # 收回 neural
        self.assertEqual(affected, ["SE-NEURAL"])
        sessions = self.c.store.collection("sessions")
        self.assertEqual(sessions["SE-NEURAL"]["status"], "invalidated")
        self.assertEqual(sessions["SE-BEHAVIOR"]["status"], "scheduled")

    def test_withdraw_invalidates_unstarted_and_disposition_retains(self):
        self._schedule("SE1")
        result = self.c.withdraw(COORD, "P01")
        self.assertEqual(result["invalidated_sessions"], ["SE1"])
        self.assertEqual(result["disposition"], "retain")
        with self.assertRaises(DomainError) as err:
            self._schedule("SE2")
        self.assertEqual(err.exception.code, "consent_inactive")

    def test_withdraw_deidentify_clears_direct_identity(self):
        self.c.enroll(
            COORD, "P02", identity={"name": "李四", "mrn": "MRN-2"},
            consent_scopes=["behavior"], withdraw_disposition="deidentify",
        )
        self.c.withdraw(COORD, "P02")
        participant = self.c.store.collection("participants")["P02"]
        self.assertEqual(participant["identity"], {})
        self.assertTrue(participant["deidentified"])
        # 分析师视图里绝不出现直接身份。
        view = self.c.analyst_view(ANALYST)
        self.assertNotIn("李四", json.dumps(view, ensure_ascii=False))

    def test_withdraw_destroy_revokes_completed_streams_in_index(self):
        self.c.enroll(
            COORD, "P03", identity={"name": "王五"},
            consent_scopes=["neural"], withdraw_disposition="destroy",
        )
        self.c.register_electrode_layout(
            COORD, "P03", "L3", [{"contact": "b1", "region": "insula"}], 1.0
        )
        self.c.schedule_session(
            COORD, "SX", "P03", 1.0, "S1", "G1", "L3", ["neural"]
        )
        self.c.start_session(COORD, "SX")
        self.c.register_raw_stream(COORD, "RX", "SX", "neural", "ecog", b"abc")
        self.c.complete_session(COORD, "SX")
        result = self.c.withdraw(COORD, "P03")
        self.assertEqual(result["streams_revoked"], ["RX"])
        self.assertEqual(
            self.c.store.collection("streams")["RX"]["status"],
            "destroyed_by_consent",
        )
        # 原始字节仍然只写不可变，撤回处置只作用于研究索引/可见性。
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "raw", "RX.bin")))

    def test_consent_history_is_versioned(self):
        self.c.update_consent(COORD, "P01", ["behavior"])
        p = self.c.store.collection("participants")["P01"]
        self.assertEqual(p["consent_version"], 2)
        self.assertEqual(p["consent_history"][-1]["removed"], ["neural", "stimuli"])


class ClinicalPriorityContractTests(CoordinatorCase):
    def test_safety_event_invalidates_unstarted_and_interrupts_running(self):
        self._schedule("SE1")
        self._schedule("SE2", start=3000.0)
        self.c.start_session(COORD, "SE1")
        outcome = self.c.raise_safety_event(
            CLIN, "EV1", "P01", "adverse_event", "患者出现恶心"
        )
        self.assertEqual(outcome["interrupted_sessions"], ["SE1"])
        self.assertEqual(outcome["invalidated_sessions"], ["SE2"])
        sessions = self.c.store.collection("sessions")
        self.assertEqual(sessions["SE1"]["status"], "interrupted")
        self.assertEqual(sessions["SE2"]["status"], "invalidated")

    def test_each_stop_reason_blocks_new_scheduling(self):
        for reason in ("medical_order_change", "participant_pause"):
            code = f"P-{reason}"
            self.c.enroll(COORD, code, consent_scopes=["behavior"])
            self.c.raise_safety_event(CLIN, f"EV-{reason}", code, reason, "x")
            with self.assertRaises(DomainError) as err:
                self.c.schedule_session(
                    COORD, f"S-{reason}", code, 1.0, "S1", "G1", "L1", ["behavior"]
                )
            self.assertEqual(err.exception.code, "clinical_hold")

    def test_invalidated_session_can_never_restart(self):
        self._schedule("SE1")
        self.c.raise_safety_event(CLIN, "EV1", "P01", "participant_pause", "暂停")
        with self.assertRaises(DomainError) as err:
            self.c.start_session(COORD, "SE1")
        self.assertEqual(err.exception.code, "invalid_state")
        self.c.clear_safety_event(CLIN, "EV1", "恢复")
        with self.assertRaises(DomainError) as err:
            self.c.start_session(COORD, "SE1")
        self.assertEqual(err.exception.code, "invalid_state")

    def test_resume_requires_cleared_event(self):
        self._schedule("SE1")
        self.c.start_session(COORD, "SE1")
        self.c.raise_safety_event(CLIN, "EV1", "P01", "adverse_event", "x")
        with self.assertRaises(DomainError) as err:
            self.c.resume_interrupted_session(COORD, "SE1")
        self.assertEqual(err.exception.code, "clinical_hold")
        self.c.clear_safety_event(CLIN, "EV1", "已缓解")
        self.c.resume_interrupted_session(COORD, "SE1")
        self.assertEqual(
            self.c.store.collection("sessions")["SE1"]["status"], "running"
        )

    def test_clinical_role_can_raise_and_clear_without_coordinator(self):
        self._schedule("SE1")
        self.c.raise_safety_event(
            CLIN, "EV1", "P01", "medical_order_change", "检查医嘱变更"
        )
        self.c.clear_safety_event(CLIN, "EV1", "新医嘱已确认")
        self.assertEqual(
            self.c.store.collection("safety_events")["EV1"]["status"], "cleared"
        )


class RawImmutabilityContractTests(CoordinatorCase):
    def _running_session_with_stream(self, sid="SE1", stream_id="R1", payload=b"raw-v1"):
        self._schedule(sid)
        self.c.start_session(COORD, sid)
        self.c.register_raw_stream(COORD, stream_id, sid, "neural", "ecog", payload)
        return stream_id

    def test_raw_stream_cannot_be_re_registered_or_overwritten(self):
        self._running_session_with_stream(sid="SE1", stream_id="R1")
        with self.assertRaises(DomainError) as err:
            self.c.register_raw_stream(COORD, "R1", "SE1", "neural", "ecog", b"other")
        self.assertEqual(err.exception.code, "conflict")
        with self.assertRaises(ValueError):
            self.c.raw.register("R1", "ecog", 0.0, b"other")

    def test_clock_correction_does_not_modify_raw(self):
        self._running_session_with_stream(payload=b"raw-bytes")
        before = self.c.raw.digest("R1")
        self.c.apply_clock_correction(
            COORD, "A1", "R1", -12.5, anchor_event="trial_1_onset"
        )
        self.assertEqual(self.c.raw.digest("R1"), before)
        alignment = self.c.store.collection("alignments")["A1"]
        self.assertTrue(alignment["derived"])
        self.assertEqual(alignment["source_sha256"], before)

    def test_segmentation_is_derived_and_versioned(self):
        self._running_session_with_stream()
        self.c.apply_clock_correction(COORD, "A1", "R1", 1.0, "onset")
        with self.assertRaises(DomainError):
            self.c.create_segmentation(
                COORD, "SG1", "R1", "A1",
                [{"segment_id": "bad", "start_ms": 100, "end_ms": 100}],
            )
        self.c.create_segmentation(
            COORD, "SG1", "R1", "A1",
            [{"segment_id": "t1", "start_ms": 0, "end_ms": 500, "label": "choice"}],
        )
        with self.assertRaises(DomainError) as err:
            self.c.create_segmentation(
                COORD, "SG1", "R1", "A1",
                [{"segment_id": "t2", "start_ms": 0, "end_ms": 1}],
            )
        self.assertEqual(err.exception.code, "conflict")

    def test_streams_blocked_when_session_not_running(self):
        self._schedule("SE1")
        with self.assertRaises(DomainError) as err:
            self.c.register_raw_stream(COORD, "R1", "SE1", "neural", "ecog", b"x")
        self.assertEqual(err.exception.code, "invalid_state")
        self.c.start_session(COORD, "SE1")
        self.c.register_raw_stream(COORD, "R1", "SE1", "neural", "ecog", b"x")
        self.c.raise_safety_event(CLIN, "EV1", "P01", "adverse_event", "x")
        with self.assertRaises(DomainError) as err:
            self.c.append_raw_stream(COORD, "R1", b"y")
        self.assertEqual(err.exception.code, "invalid_state")

    def test_tampering_with_raw_bytes_is_detected(self):
        self._running_session_with_stream(payload=b"raw-secret")
        self.c.apply_clock_correction(COORD, "A1", "R1", 2.0, "onset")
        self.c.create_segmentation(
            COORD, "SG1", "R1", "A1",
            [{"segment_id": "t1", "start_ms": 0, "end_ms": 10}],
        )
        self.c.complete_session(COORD, "SE1")
        self.c.preregister_hypothesis(
            ANALYST, "H1", "假设", "pos", "lmm", ["SE1"], ["neural"]
        )
        self.c.create_exclusion_log(ANALYST, "E1", "H1", [], "v1")
        self.c.run_analysis(
            ANALYST, "AN1", "H1", "confirmatory", "E1", ["A1"], ["SG1"]
        )
        path = os.path.join(self.tmp, "raw", "R1.bin")
        with open(path, "wb") as handle:
            handle.write(b"TAMPERED!!!")
        report = self.c.reproducibility_report("AN1")
        self.assertFalse(report["all_passed"])
        failed = {c["check"] for c in report["checks"] if not c["passed"]}
        self.assertIn("alignment_source_hash", failed)


class AnalysisContractTests(CoordinatorCase):
    def _completed_session_pipeline(self, sid="SE1", stream_id="R1"):
        alignment_id = f"A-{stream_id}"
        segmentation_id = f"SG-{stream_id}"
        self._schedule(sid)
        self.c.start_session(COORD, sid)
        self.c.register_raw_stream(COORD, stream_id, sid, "neural", "ecog", b"raw")
        self.c.apply_clock_correction(
            COORD, alignment_id, stream_id, -3.0, "onset"
        )
        self.c.create_segmentation(
            COORD, segmentation_id, stream_id, alignment_id,
            [{"segment_id": "t1", "start_ms": 0, "end_ms": 100}],
        )
        self.c.complete_session(COORD, sid)
        return alignment_id, segmentation_id

    def test_analysis_must_reference_preregistration_and_fixed_range(self):
        self._completed_session_pipeline()
        with self.assertRaises(DomainError) as err:
            self.c.run_analysis(
                ANALYST, "AN1", "MISSING-H", "confirmatory",
                exclusion_id=None,
            )
        self.assertEqual(err.exception.code, "not_found")

    def test_confirmatory_analysis_requires_exclusion_log(self):
        self._completed_session_pipeline()
        self.c.preregister_hypothesis(
            ANALYST, "H1", "h", "pos", "lmm", ["SE1"], ["neural"]
        )
        with self.assertRaises(DomainError) as err:
            self.c.run_analysis(ANALYST, "AN1", "H1", "confirmatory")
        self.assertEqual(err.exception.code, "missing_exclusion")

    def test_fixed_range_is_frozen_and_exclusion_replays(self):
        self._completed_session_pipeline("SE1")
        self._completed_session_pipeline("SE2", "R2")
        self.c.preregister_hypothesis(
            ANALYST, "H1", "h", "pos", "lmm", ["SE1", "SE2"], ["neural"]
        )
        self.c.create_exclusion_log(
            ANALYST, "E1", "H1",
            [{"session_id": "SE2", "rule": "artifact", "reason": "伪迹超限"}],
            "rules-v1",
        )
        analysis = self.c.run_analysis(
            ANALYST, "AN1", "H1", "confirmatory", "E1", ["A-R1"], ["SG-R1"]
        )
        self.assertEqual(analysis["included_session_ids"], ["SE1"])
        # 排除记录引用范围外环节必须拒绝。
        self._completed_session_pipeline("SE3", "R3")
        self.c.create_exclusion_log(
            ANALYST, "E2", "H1",
            [{"session_id": "SE3", "rule": "x", "reason": "out of range"}],
            "rules-v1",
        )
        with self.assertRaises(DomainError) as err:
            self.c.run_analysis(ANALYST, "AN2", "H1", "confirmatory", "E2")
        self.assertEqual(err.exception.code, "range_broken")

    def test_derivatives_outside_fixed_range_rejected(self):
        self._completed_session_pipeline("SE1")
        self._completed_session_pipeline("SE2", "R2")
        self.c.preregister_hypothesis(
            ANALYST, "H1", "h", "pos", "lmm", ["SE1"], ["neural"]
        )
        self.c.create_exclusion_log(ANALYST, "E1", "H1", [], "v1")
        with self.assertRaises(DomainError) as err:
            self.c.run_analysis(
                ANALYST, "AN1", "H1", "confirmatory", "E1",
                alignment_ids=[], segmentation_ids=["SG-R2"],
            )
        self.assertEqual(err.exception.code, "range_broken")

    def test_exploratory_results_cannot_be_released(self):
        self._completed_session_pipeline()
        self.c.preregister_hypothesis(
            ANALYST, "H1", "h", "pos", "lmm", ["SE1"], ["neural"]
        )
        self.c.run_analysis(
            ANALYST, "AN-X", "H1", "exploratory", result={"p": 0.001}
        )
        with self.assertRaises(DomainError) as err:
            self.c.release_publication(ANALYST, "PUB-X", "AN-X", "do")
        self.assertEqual(err.exception.code, "exploratory_blocked")
        self.assertEqual(
            self.c.store.collection("analyses")["AN-X"]["zone"], "exploratory"
        )

    def test_confirmatory_release_passes_reproducibility_gate(self):
        self._completed_session_pipeline()
        self.c.preregister_hypothesis(
            ANALYST, "H1", "h", "pos", "lmm", ["SE1"], ["neural"]
        )
        self.c.create_exclusion_log(ANALYST, "E1", "H1", [], "v1")
        self.c.run_analysis(
            ANALYST, "AN1", "H1", "confirmatory", "E1", ["A-R1"], ["SG-R1"],
            result={"estimate": 0.42},
        )
        pub = self.c.release_publication(ANALYST, "PUB1", "AN1", "do")
        self.assertEqual(pub["decision_label"], "do")
        self.assertTrue(pub["reproducibility"]["all_passed"])
        checks = {c["check"] for c in pub["reproducibility"]["checks"]}
        self.assertEqual(
            checks,
            {
                "range_session_status",
                "exclusion_replay",
                "alignment_source_hash",
                "segmentation_source_hash",
                "analysis_versions_enumerable",
                "audit_chain",
            },
        )

    def test_multiple_analysis_versions_are_all_enumerable(self):
        self._completed_session_pipeline()
        self.c.preregister_hypothesis(
            ANALYST, "H1", "h", "pos", "lmm", ["SE1"], ["neural"]
        )
        self.c.create_exclusion_log(ANALYST, "E1", "H1", [], "v1")
        self.c.run_analysis(ANALYST, "AN1", "H1", "confirmatory", "E1")
        self.c.run_analysis(
            ANALYST, "AN2", "H1", "exploratory", result={"note": "试出来的"}
        )
        report = self.c.reproducibility_report("AN1")
        versions = next(
            c["versions"] for c in report["checks"]
            if c["check"] == "analysis_versions_enumerable"
        )
        self.assertEqual(versions, ["AN1", "AN2"])


class RoleIsolationContractTests(CoordinatorCase):
    def test_clinical_view_has_safety_but_no_research_inference(self):
        self._schedule("SE1")
        self.c.raise_safety_event(CLIN, "EV1", "P01", "adverse_event", "恶心")
        view = self.c.clinical_safety_view(CLIN)
        text = json.dumps(view, ensure_ascii=False)
        self.assertIn("EV1", text)
        self.assertNotIn("hypotheses", text)
        self.assertNotIn("result", text)

    def test_analyst_view_is_de_identified(self):
        view = self.c.analyst_view(ANALYST)
        text = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("张三", text)
        self.assertNotIn("MRN-1", text)
        for p in view["participants"]:
            self.assertNotIn("identity", p)

    def test_cross_role_access_forbidden(self):
        for fn, token in (
            (lambda: self.c.analyst_view(CLIN), CLIN),
            (lambda: self.c.clinical_safety_view(ANALYST), ANALYST),
            (lambda: self.c.session_status_board(CLIN), CLIN),
        ):
            with self.assertRaises(DomainError) as err:
                fn()
            self.assertEqual(err.exception.code, "forbidden")

    def test_unknown_token_unauthorized(self):
        with self.assertRaises(DomainError) as err:
            self.c.session_status_board("nobody")
        self.assertEqual(err.exception.code, "unauthorized")

    def test_audit_chain_covers_all_actions_and_verifies_after_restart(self):
        self._schedule("SE1")
        self.c.start_session(COORD, "SE1")
        self.c.register_raw_stream(COORD, "R1", "SE1", "neural", "ecog", b"x")
        self.c.raise_safety_event(CLIN, "EV1", "P01", "adverse_event", "x")
        ok, broken = self.c.audit.verify_chain()
        self.assertTrue(ok, broken)
        restarted = Coordinator(self.tmp)
        ok2, broken2 = restarted.audit.verify_chain()
        self.assertTrue(ok2, broken2)
        self.assertGreater(restarted.audit.count(), 0)


class GameConfigContractTests(CoordinatorCase):
    def test_probabilities_must_sum_to_one(self):
        with self.assertRaises(DomainError):
            self.c.register_game_config(COORD, "GBAD", 0.6, 0.5)
        with self.assertRaises(DomainError):
            self.c.register_game_config(COORD, "GBAD2", -0.1, 1.1)

    def test_layout_supersession(self):
        self.c.register_electrode_layout(
            COORD, "P01", "L2", [{"contact": "c1", "region": "ofc"}], 2.0
        )
        self.c.supersede_layout(COORD, "L1", "L2")
        self.assertEqual(
            self.c.store.collection("layouts")["L1"]["superseded_by"], "L2"
        )


class HttpContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(cls.tmp))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _call(self, method, path, token=None, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = Request(self.base_url + path, data=data, method=method,
                          headers=headers)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_health_contract_preserved(self):
        status, body = self._call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {"status": "ok", "service": "intracranial-decision-study",
             "name": "颅内决策实验编排"},
        )

    def test_full_clinical_workflow_over_http(self):
        status, _ = self._call("POST", "/api/participants", COORD, {
            "code": "P01", "identity": {"mrn": "M1"},
            "consent_scopes": ["behavior", "neural", "stimuli"],
        })
        self.assertEqual(status, 200)
        status, _ = self._call("POST", "/api/layouts", COORD, {
            "participant_code": "P01", "layout_id": "L1",
            "contacts": [{"contact": "a1", "region": "amygdala"}],
            "implanted_at": 1.0,
        })
        self.assertEqual(status, 200)
        status, _ = self._call("POST", "/api/stimulus-sets", COORD, {
            "stimulus_set_id": "S1", "items": [{"id": "i1"}],
        })
        self.assertEqual(status, 200)
        status, _ = self._call("POST", "/api/game-configs", COORD, {
            "config_id": "G1", "gem_probability": 0.6, "bomb_probability": 0.4,
        })
        self.assertEqual(status, 200)
        status, body = self._call("POST", "/api/sessions", COORD, {
            "session_id": "SE1", "participant_code": "P01", "planned_start": 1.0,
            "stimulus_set_id": "S1", "config_id": "G1", "layout_id": "L1",
            "required_scopes": ["neural"],
        })
        self.assertEqual(status, 200, body)
        status, _ = self._call("POST", "/api/sessions/SE1/start", COORD)
        self.assertEqual(status, 200)
        status, body = self._call("POST", "/api/streams", COORD, {
            "stream_id": "R1", "session_id": "SE1", "scope": "neural",
            "label": "ecog", "payload_base64": "cmF3",
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["bytes"], 3)
        status, body = self._call("POST", "/api/safety-events", CLIN, {
            "event_id": "EV1", "participant_code": "P01",
            "reason": "adverse_event", "message": "恶心",
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["interrupted_sessions"], ["SE1"])
        status, body = self._call("GET", "/api/safety", CLIN)
        self.assertEqual(status, 200)
        self.assertEqual(body["safety_events"][0]["event_id"], "EV1")

    def test_http_role_enforcement_and_json_errors(self):
        status, body = self._call("GET", "/api/sessions")
        self.assertEqual(status, 401)
        status, body = self._call("GET", "/api/sessions", ANALYST)
        self.assertEqual(status, 403)
        status, body = self._call("GET", "/api/nope", COORD)
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
