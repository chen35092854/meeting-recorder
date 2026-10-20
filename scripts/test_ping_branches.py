# -*- coding: utf-8 -*-
"""ping() 分支测试:用本地 mock HTTP 服务覆盖各错误分类。

覆盖分支:
  1. 200 + 有效 choices   → ✓ 连接成功:鉴权与模型均正常
  2. 200 + 无 choices     → ✓ 连接成功(HTTP 200)
  3. 200 + 非法 JSON      → ✓ 连接成功(HTTP 200)
  4. 401                 → 接口拒绝访问(HTTP 401)——Key 错误
  5. 404                 → 接口路径似乎不对(HTTP 404)
  6. 500                 → 接口返回异常(HTTP 500)
  7. 连接失败(端口不通)    → 无法连接接口(网络/代理/地址问题)
  8. 空 Key              → 请先填写 API Key(不入网)
  9. URL 已含 /chat/completions 后缀 → 不重复拼接(路径正确)
  10. ssl_verify=False    → 请求正常发出(走 http 无差异,验证参数透传)
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import summarize  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")


class Handler(BaseHTTPRequestHandler):
    mode = "ok"          # ok | no_choices | bad_json | 401 | 404 | 500
    seen_path = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        type(self).seen_path = self.path
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = b""
        code = 200
        mode = type(self).mode
        if mode == "no_choices":
            body = json.dumps({"foo": 1}).encode()
        elif mode == "bad_json":
            body = b"not-json{{{"
        elif mode == "401":
            code, body = 401, json.dumps({"error": "unauthorized"}).encode()
        elif mode == "404":
            code, body = 404, json.dumps({"error": "not found"}).encode()
        elif mode == "500":
            code, body = 500, json.dumps({"error": "boom"}).encode()
        else:
            body = json.dumps({"choices": [
                {"message": {"content": "ok"}, "finish_reason": "stop"}]}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_case(name: str, mode: str, **kwargs):
    Handler.mode = mode
    ok, msg = summarize.ping(
        f"http://127.0.0.1:{PORT}", "sk-test-123", "gpt-test", **kwargs)
    print(f"  case[{name}] ok={ok} msg={msg!r}")
    return ok, msg


if __name__ == "__main__":
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    PORT = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print(f"mock server on 127.0.0.1:{PORT}")

    # 1. 正常 200 + choices
    ok, msg = run_case("200_choices", "ok")
    check("200+choices 判定成功", ok and "鉴权与模型均正常" in msg, msg)

    # 2. 200 无 choices
    ok, msg = run_case("200_no_choices", "no_choices")
    check("200+无choices 判定成功", ok and "HTTP 200" in msg, msg)

    # 3. 200 非法 JSON
    ok, msg = run_case("200_bad_json", "bad_json")
    check("200+非法JSON 判定成功(不崩溃)", ok and "HTTP 200" in msg, msg)

    # 4. 401
    ok, msg = run_case("401", "401")
    check("401→Key错误提示", (not ok) and "401" in msg and "API Key" in msg, msg)

    # 5. 404
    ok, msg = run_case("404", "404")
    check("404→路径错误提示", (not ok) and "404" in msg and "接口地址" in msg, msg)

    # 6. 500
    ok, msg = run_case("500", "500")
    check("500→异常提示", (not ok) and "500" in msg, msg)

    # 7. 连接失败:bind 但不监听 → connect 必得 ECONNREFUSED(确定空闲)
    import socket
    tmp = socket.socket()
    tmp.bind(("127.0.0.1", 0))
    dead_port = tmp.getsockname()[1]
    tmp.close()  # 不 listen,close 后 connect 会被拒
    ok, msg = summarize.ping(
        f"http://127.0.0.1:{dead_port}", "sk-x", "m")
    check("连接失败→网络提示", (not ok) and "无法连接" in msg, msg)

    # 8. 空 Key
    ok, msg = summarize.ping(f"http://127.0.0.1:{PORT}", "", "m")
    check("空Key→提示填写", (not ok) and "API Key" in msg, msg)

    # 9. URL 已含 /chat/completions → 路径不重复
    Handler.seen_path = None
    ok, msg = summarize.ping(
        f"http://127.0.0.1:{PORT}/chat/completions", "sk-x", "m")
    check("自带后缀不重复拼接", Handler.seen_path == "/chat/completions",
          f"seen_path={Handler.seen_path!r}")

    # 10. verify_ssl=False 透传(不影响 http 请求)
    Handler.mode = "ok"
    Handler.seen_path = None
    ok, msg = summarize.ping(
        f"http://127.0.0.1:{PORT}", "sk-x", "m", verify_ssl=False)
    check("verify_ssl=False 请求正常", ok, msg)

    srv.shutdown()
    srv.server_close()
    print(f"\n=== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ===")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)
    print("全部通过 ✓")
