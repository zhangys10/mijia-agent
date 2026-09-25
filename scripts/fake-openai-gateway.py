"""Loopback-only OpenAI-compatible fixture for local assistant integration tests."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def create_handler(tool_name: str, room: str, metric: str, kind: str):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 1_000_000:
                    self.send_error(413)
                    return
                request = json.loads(self.rfile.read(length))
                messages = request.get("messages", [])
                tools = request.get("tools", [])
                available = {
                    tool.get("function", {}).get("name") for tool in tools if isinstance(tool, dict)
                }
                has_tool_result = any(message.get("role") == "tool" for message in messages)
                if not has_tool_result and tool_name in available:
                    arguments = (
                        {"rooms": [room], "metrics": [metric]}
                        if tool_name == "get_home_environment"
                        else {"rooms": [room], "kinds": [kind], "states": ["on", "off", "unknown"]}
                    )
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_local_fixture_1",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(arguments, ensure_ascii=False),
                                },
                            }
                        ],
                    }
                    print(f"fake gateway: scripted tool call {tool_name}", flush=True)
                elif has_tool_result:
                    message = {
                        "role": "assistant",
                        "content": "本地集成测试完成：假模型收到了家庭读取工具结果。",
                    }
                    print("fake gateway: scripted final answer", flush=True)
                else:
                    message = {
                        "role": "assistant",
                        "content": "当前家庭没有开放 fixture 请求的读取能力。",
                    }
                    print("fake gateway: requested home capability unavailable", flush=True)
                response = {
                    "id": "chatcmpl_local_fixture",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
                }
                payload = json.dumps(response, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (ValueError, TypeError, AttributeError):
                self.send_error(400)

        def log_message(self, _format, *_args):
            # Do not print request bodies, prompts, or authorization headers.
            return

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9901)
    parser.add_argument(
        "--tool",
        choices=("get_home_environment", "get_device_status"),
        default="get_home_environment",
    )
    parser.add_argument("--room", default="客厅")
    parser.add_argument("--metric", default="temperature")
    parser.add_argument("--kind", default="light")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port), create_handler(args.tool, args.room, args.metric, args.kind)
    )
    print(
        f"fake gateway listening on http://127.0.0.1:{args.port}/v1 (no model provider calls)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
