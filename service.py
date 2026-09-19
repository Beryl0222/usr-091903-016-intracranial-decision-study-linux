"""颅内决策实验编排的运行入口。

默认提供稳定的健康检查；指定 --data-dir 后挂载研究协调领域接口。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from study.api import make_handler
from study.coordinator import Coordinator

SERVICE_ID = "intracranial-decision-study"
SERVICE_NAME = "颅内决策实验编排"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """仅提供健康检查，保持基础契约稳定（测试直接依赖）。"""

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps(health_payload(), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def build_handler(data_dir=None):
    """领域接口处理器；data_dir 为 None 时退化为纯健康检查。"""
    if data_dir is None:
        return Handler
    coordinator = Coordinator(data_dir)
    return make_handler(coordinator)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="持久化目录；提供后启用研究协调领域接口",
    )
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    handler_cls = build_handler(args.data_dir)
    ThreadingHTTPServer(("0.0.0.0", args.port), handler_cls).serve_forever()


if __name__ == "__main__":
    main()
