"""HTTP 接口端到端测试：角色头、路由与错误映射。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service
from study.api import Api
from study.system import StudySystem

T0 = 1_700_000_000_000
HOUR = 3_600_000


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._previous_api = service.Handler.api
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        service.Handler.api = cls._previous_api

    def setUp(self):
        self.clock = Clock()
        service.Handler.api = Api(StudySystem(now_ms=self.clock))

    # --------------------------------------------------------------
    def call(self, method, path, role=None, body=None, actor="tester"):
        headers = {"Content-Type": "application/json"}
        if role:
            headers["X-Actor-Role"] = role
            headers["X-Actor-Id"] = actor
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            payload = json.load(error)
            error.close()
            return error.code, payload

    def enroll_fixture(self):
        status, _ = self.call("POST", "/participants", "coordinator", {
            "participant_id": "P01",
            "identity": {"legal_name": "患者甲", "medical_record_number": "MRN-001"},
            "inpatient_start_ms": T0,
            "inpatient_end_ms": T0 + 72 * HOUR,
        })
        self.assertEqual(status, 200)
        self.call("POST", "/participants/P01/consents", "coordinator", {
            "scopes": ["behavior", "neural", "storage", "publication"],
            "effective_from_ms": T0,
            "effective_to_ms": T0 + 72 * HOUR,
            "completed_data_policy": "retain",
        })
        self.call("POST", "/participants/P01/electrodes", "coordinator", {
            "contacts": [{"contact_id": "A1", "region": "OFC", "x": 1, "y": 2, "z": 3}],
            "method": "术后CT配准",
            "effective_ms": T0,
        })
        self.call("POST", "/stimulus-sets", "coordinator", {
            "version": "stim-1.0",
            "trial_types": [{"name": "std", "gem_probability": 0.7, "bomb_probability": 0.3}],
            "timing": {"cue_ms": 500, "decision_ms": 1500},
        })

    def test_full_research_workflow(self):
        self.enroll_fixture()
        # 临床占时优先：监测块之外的空档才能排程
        status, block = self.call("POST", "/participants/P01/clinical-blocks", "clinician", {
            "kind": "monitoring", "start_ms": T0 + 3 * HOUR, "end_ms": T0 + 4 * HOUR,
        })
        self.assertEqual(status, 200)
        status, conflict = self.call("POST", "/participants/P01/sessions", "coordinator", {
            "start_ms": T0 + 3 * HOUR + 1, "end_ms": T0 + 5 * HOUR, "stimulus_version": "stim-1.0",
        })
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"], "ClinicalConflict")

        # 预注册假设先于数据采集
        status, hyp = self.call("POST", "/hypotheses", "analyst", {
            "statement": "OFC 活动预测风险选择",
            "data_range": {
                "participant_ids": ["P01"],
                "session_window": [T0, T0 + 72 * HOUR],
                "stimulus_versions": ["stim-1.0"],
            },
            "analysis_plan": "logistic 回归",
            "code_version": "v1",
        })
        self.assertEqual(status, 200)
        hypothesis_id = hyp["data"]["hypothesis_id"]

        status, ses = self.call("POST", "/participants/P01/sessions", "coordinator", {
            "start_ms": T0 + 5 * HOUR, "end_ms": T0 + 6 * HOUR, "stimulus_version": "stim-1.0",
        })
        session_id = ses["data"]["session_id"]
        self.clock.t = T0 + 5 * HOUR
        self.call("POST", f"/sessions/{session_id}/start", "coordinator")
        status, beh = self.call("POST", f"/sessions/{session_id}/streams", "coordinator", {
            "device": "behavior-rig", "clock_id": "clk-beh", "started_ms": T0 + 5 * HOUR,
            "records": [[0, {"event": "cue"}], [1500, {"event": "choice"}]],
        })
        status, neu = self.call("POST", f"/sessions/{session_id}/streams", "coordinator", {
            "device": "neural-amp", "clock_id": "clk-neu", "started_ms": T0 + 5 * HOUR,
            "records": [[0, {"ch": "A1", "v": 0.1}]],
        })
        self.clock.t = T0 + 6 * HOUR
        self.call("POST", f"/sessions/{session_id}/complete", "coordinator")

        # 时钟校正与分段不改动原始流
        raw_hash = beh["data"]["content_hash"]
        status, beh_corr = self.call(
            "POST", f"/streams/{beh['data']['stream_id']}/clock-corrections", "analyst",
            {"offset_ms": 2.0, "drift_ppm": 0.0, "anchors": [[0, 2]], "reason": "校正"},
        )
        status, neu_corr = self.call(
            "POST", f"/streams/{neu['data']['stream_id']}/clock-corrections", "analyst",
            {"offset_ms": -5.0, "drift_ppm": 0.0, "anchors": [[0, -5]], "reason": "校正"},
        )
        status, seg = self.call(
            "POST", f"/streams/{beh['data']['stream_id']}/segments", "analyst",
            {"label": "决策窗", "correction_id": beh_corr["data"]["correction_id"],
             "start_device_ms": 0, "end_device_ms": 1500},
        )
        status, extracted = self.call(
            "GET", f"/segments/{seg['data']['segment_id']}/extract", "analyst"
        )
        self.assertEqual(extracted["data"]["records"][0][0], 2.0)

        status, aln = self.call("POST", "/alignments", "analyst", {
            "behavior_stream_id": beh["data"]["stream_id"],
            "neural_stream_id": neu["data"]["stream_id"],
            "behavior_correction_id": beh_corr["data"]["correction_id"],
            "neural_correction_id": neu_corr["data"]["correction_id"],
        })
        alignment_id = aln["data"]["alignment_id"]

        status, exc = self.call("POST", "/exclusions", "analyst", {
            "target_kind": "trial", "target_id": "trial-7",
            "reason": "肌电伪迹", "code": "ARTIFACT",
        })
        exclusion_id = exc["data"]["exclusion_id"]

        self.clock.t = T0 + 7 * HOUR
        status, ana = self.call("POST", "/analyses", "analyst", {
            "data_range": {
                "participant_ids": ["P01"],
                "session_window": [T0, T0 + 72 * HOUR],
                "stimulus_versions": ["stim-1.0"],
            },
            "code_version": "v1",
            "electrode_versions": ["P01#v1"],
            "parameters": {"model": "logistic"},
            "result_summary": {"effect": 0.42},
            "exclusion_ids": [exclusion_id],
            "alignment_ids": [alignment_id],
            "hypothesis_id": hypothesis_id,
        })
        self.assertEqual(status, 200, ana)
        self.assertEqual(ana["data"]["zone"], "confirmatory")

        status, pub = self.call("POST", "/publications", "analyst", {
            "analysis_ids": [ana["data"]["analysis_id"]],
            "conclusions": [{"region": "OFC", "recommendation": "go"}],
        })
        self.assertEqual(status, 200, pub)
        status, report = self.call(
            "GET", f"/publications/{pub['data']['bundle_id']}/verify", "analyst"
        )
        self.assertTrue(report["data"]["ok"], report)

        # 原始流哈希全程未变
        status, view = self.call("GET", "/participants/P01", "analyst")
        self.assertNotIn("identity", view["data"])
        self.assertNotIn("患者甲", json.dumps(view["data"], ensure_ascii=False))
        status, journal = self.call("GET", "/journal/verify", "coordinator")
        self.assertEqual(journal["data"]["mismatches"], [])

    def test_role_based_visibility(self):
        self.enroll_fixture()
        status, _ = self.call("POST", "/participants/P01/adverse-events", "clinician", {
            "severity": "mild", "description": "头痛", "occurred_ms": T0 + HOUR,
        })
        self.assertEqual(status, 200)
        status, safety = self.call("GET", "/participants/P01/safety", "clinician")
        self.assertEqual(status, 200)
        self.assertEqual(safety["data"]["identity"]["legal_name"], "患者甲")
        self.assertEqual(len(safety["data"]["safety_events"]), 1)
        # 临床人员不能读取研究推断
        status, denied = self.call("GET", "/analyses/ana-0001", "clinician")
        self.assertEqual(status, 403)
        # 分析人员不能查看安全视图
        status, denied = self.call("GET", "/participants/P01/safety", "analyst")
        self.assertEqual(status, 403)

    def test_adverse_event_invalidates_unstarted_session(self):
        self.enroll_fixture()
        status, ses = self.call("POST", "/participants/P01/sessions", "coordinator", {
            "start_ms": T0 + 10 * HOUR, "end_ms": T0 + 11 * HOUR,
            "stimulus_version": "stim-1.0",
        })
        session_id = ses["data"]["session_id"]
        status, event = self.call("POST", "/participants/P01/adverse-events", "clinician", {
            "severity": "severe", "description": "癫痫样放电", "occurred_ms": T0 + 2 * HOUR,
        })
        self.assertEqual(status, 200)
        self.assertIn(session_id, event["data"]["affected_sessions"])
        status, view = self.call("GET", "/participants/P01", "coordinator")
        statuses = {s["session_id"]: s["status"] for s in view["data"]["sessions"]}
        self.assertEqual(statuses[session_id], "invalidated")

    def test_late_hypothesis_rejected_as_confirmatory(self):
        self.enroll_fixture()
        status, ses = self.call("POST", "/participants/P01/sessions", "coordinator", {
            "start_ms": T0 + HOUR, "end_ms": T0 + 2 * HOUR, "stimulus_version": "stim-1.0",
        })
        session_id = ses["data"]["session_id"]
        self.clock.t = T0 + HOUR
        self.call("POST", f"/sessions/{session_id}/start", "coordinator")
        self.clock.t = T0 + 2 * HOUR
        self.call("POST", f"/sessions/{session_id}/complete", "coordinator")
        self.clock.t = T0 + 3 * HOUR
        status, hyp = self.call("POST", "/hypotheses", "analyst", {
            "statement": "事后假设",
            "data_range": {
                "participant_ids": ["P01"],
                "session_window": [T0, T0 + 72 * HOUR],
                "stimulus_versions": ["stim-1.0"],
            },
            "analysis_plan": "plan",
            "code_version": "v1",
        })
        status, error = self.call("POST", "/analyses", "analyst", {
            "data_range": {
                "participant_ids": ["P01"],
                "session_window": [T0, T0 + 72 * HOUR],
                "stimulus_versions": ["stim-1.0"],
            },
            "code_version": "v1",
            "electrode_versions": ["P01#v1"],
            "parameters": {},
            "result_summary": {},
            "hypothesis_id": hyp["data"]["hypothesis_id"],
        })
        self.assertEqual(status, 409)
        self.assertEqual(error["error"], "GovernanceError")

    def test_missing_role_and_unknown_route(self):
        status, error = self.call("POST", "/participants", None, {"participant_id": "P09"})
        self.assertEqual(status, 403)
        status, _ = self.call("GET", "/nope", "coordinator")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
