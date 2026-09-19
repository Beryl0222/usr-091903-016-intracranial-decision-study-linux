"""HTTP 路由：把 JSON 请求映射到 Coordinator，实施令牌与角色隔离。

令牌通过 Authorization: Bearer <token> 或 X-Auth-Token 传递。
"""

import base64
import binascii
import json
import re
from http.server import BaseHTTPRequestHandler

from study.coordinator import DomainError

# 状态码映射 ---------------------------------------------------------------

_STATUS_BY_CODE = {
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
    "invalid_input": 400,
    "invalid_state": 409,
    "clinical_hold": 409,
    "consent_inactive": 409,
    "consent_scope": 403,
    "missing_exclusion": 422,
    "range_broken": 422,
    "exploratory_blocked": 422,
    "reproducibility_failed": 422,
}

# 路由表：(HTTP 方法, 路径正则, coordinator 方法名, 允许角色, 路径参数名)
ROUTES = [
    ("POST", r"^/api/participants$", "enroll", ("coordinator",), []),
    ("POST", r"^/api/participants/(?P<code>[^/]+)/consent$",
     "update_consent", ("coordinator",), ["code"]),
    ("POST", r"^/api/participants/(?P<code>[^/]+)/withdraw$",
     "withdraw", ("coordinator",), ["code"]),

    ("POST", r"^/api/layouts$",
     "register_electrode_layout", ("coordinator",), []),
    ("POST", r"^/api/layouts/(?P<old_layout_id>[^/]+)/supersede$",
     "supersede_layout", ("coordinator",), ["old_layout_id"]),

    ("POST", r"^/api/stimulus-sets$",
     "register_stimulus_set", ("coordinator",), []),
    ("POST", r"^/api/game-configs$",
     "register_game_config", ("coordinator",), []),

    ("POST", r"^/api/sessions$", "schedule_session", ("coordinator",), []),
    ("GET", r"^/api/sessions$",
     "session_status_board", ("coordinator",), []),
    ("POST", r"^/api/sessions/(?P<session_id>[^/]+)/start$",
     "start_session", ("coordinator",), ["session_id"]),
    ("POST", r"^/api/sessions/(?P<session_id>[^/]+)/complete$",
     "complete_session", ("coordinator",), ["session_id"]),
    ("POST", r"^/api/sessions/(?P<session_id>[^/]+)/resume$",
     "resume_interrupted_session", ("coordinator",), ["session_id"]),

    ("POST", r"^/api/safety-events$",
     "raise_safety_event", ("coordinator", "clinical"), []),
    ("POST", r"^/api/safety-events/(?P<event_id>[^/]+)/clear$",
     "clear_safety_event", ("coordinator", "clinical"), ["event_id"]),
    ("GET", r"^/api/safety$",
     "clinical_safety_view", ("coordinator", "clinical"), []),

    ("POST", r"^/api/streams$",
     "register_raw_stream", ("coordinator",), []),
    ("POST", r"^/api/streams/(?P<stream_id>[^/]+)/append$",
     "append_raw_stream", ("coordinator",), ["stream_id"]),
    ("POST", r"^/api/alignments$",
     "apply_clock_correction", ("coordinator",), []),
    ("POST", r"^/api/segmentations$",
     "create_segmentation", ("coordinator",), []),

    ("POST", r"^/api/hypotheses$",
     "preregister_hypothesis", ("coordinator", "analyst"), []),
    ("POST", r"^/api/exclusion-logs$",
     "create_exclusion_log", ("coordinator", "analyst"), []),
    ("POST", r"^/api/analyses$", "run_analysis", ("coordinator", "analyst"), []),
    ("GET", r"^/api/analyses/(?P<analysis_id>[^/]+)/reproducibility$",
     "reproducibility_report", ("coordinator", "analyst"), ["analysis_id"]),
    ("POST", r"^/api/publications$",
     "release_publication", ("coordinator", "analyst"), []),

    ("GET", r"^/api/clinical-view$",
     "clinical_safety_view", ("coordinator", "clinical"), []),
    ("GET", r"^/api/analyst-view$",
     "analyst_view", ("coordinator", "analyst"), []),
]

# 请求体中以 base64 承载的字节字段 -> coordinator 关键字参数。
BASE64_FIELDS = {"payload_base64": "payload"}


def make_handler(coordinator):
    """生成绑定到指定 Coordinator 的请求处理器类。"""

    class ApiHandler(BaseHTTPRequestHandler):
        server_version = "IntracranialStudy/1.0"

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method):
            path = self.path.split("?", 1)[0]
            query = self._parse_query()
            if path == "/health":
                self._write_json(200, _health())
                return
            for route_method, pattern, action, roles, path_params in ROUTES:
                if route_method != method:
                    continue
                match = re.match(pattern, path)
                if match is None:
                    continue
                self._handle_route(action, roles, path_params, match, query)
                return
            self._write_json(404, {"error": f"未找到路由：{method} {path}",
                                   "code": "not_found"})

        def _handle_route(self, action, roles, path_params, match, query):
            token = self._extract_token()
            kwargs = {name: match.group(name) for name in path_params}
            if self.command == "POST":
                try:
                    kwargs.update(self._read_body())
                except ValueError as exc:
                    self._write_json(400, {"error": str(exc), "code": "invalid_input"})
                    return
                self._decode_binary_fields(kwargs)
            else:
                kwargs.update(query)
            # 角色校验作为统一入口（coordinator 内仍二次校验，绝不信任路由层）。
            try:
                with coordinator.lock:
                    coordinator.require_role(token, *roles)
                    result = getattr(coordinator, action)(token, **kwargs)
            except DomainError as exc:
                self._write_json(
                    _STATUS_BY_CODE.get(exc.code, 400),
                    {"error": str(exc), "code": exc.code},
                )
                return
            except (TypeError, binascii.Error) as exc:
                self._write_json(400, {"error": f"请求参数错误：{exc}",
                                       "code": "invalid_input"})
                return
            self._write_json(200, result)

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return body

        @staticmethod
        def _decode_binary_fields(kwargs):
            for encoded_key, binary_key in BASE64_FIELDS.items():
                if encoded_key in kwargs:
                    kwargs[binary_key] = base64.b64decode(kwargs.pop(encoded_key))

        def _extract_token(self):
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth[len("Bearer "):].strip()
            return self.headers.get("X-Auth-Token", "")

        def _parse_query(self):
            if "?" not in self.path:
                return {}
            from urllib.parse import parse_qs
            parsed = parse_qs(self.path.split("?", 1)[1])
            return {key: values[-1] for key, values in parsed.items()}

        def _write_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    return ApiHandler


def _health():
    # 延迟导入以避免与 service.py 形成循环。
    from service import health_payload
    return health_payload()
