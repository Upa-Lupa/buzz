"""Read-only live smoke: real ACP agent + document MCP + existing Buzz attachment.

Does not publish chat messages or expose any ERP write tools.
Requires the same provider environment as start-local.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from bech32 import bech32_encode, convertbits

from relay_documents import parse_secret


async def run(channel, message):
    root = Path(__file__).resolve().parent
    process = await asyncio.create_subprocess_exec(
        str(root.parent / "target/debug/buzz-agent"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=2 * 1024 * 1024,
    )

    async def send(value):
        process.stdin.write((json.dumps(value) + "\n").encode())
        await process.stdin.drain()

    async def rpc(request_id, method, params):
        await send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        while True:
            line = await process.stdout.readline()
            if not line:
                raise RuntimeError("Agent exited before response")
            frame = json.loads(line)
            if frame.get("id") == request_id and "method" not in frame:
                if "error" in frame:
                    raise RuntimeError(frame["error"])
                return frame["result"]
            if frame.get("method") == "session/request_permission":
                options = frame["params"].get("options", [])
                option = next(o for o in options if o["kind"] == "allow_once")
                await send(
                    {
                        "jsonrpc": "2.0",
                        "id": frame["id"],
                        "result": {
                            "outcome": {
                                "outcome": "selected",
                                "optionId": option["optionId"],
                            }
                        },
                    }
                )
            update = frame.get("params", {}).get("update", {})
            if update.get("sessionUpdate") == "tool_call":
                print("TOOL", update.get("title"), flush=True)
            if update.get("sessionUpdate") == "agent_message_chunk":
                print(update.get("content", {}).get("text", ""), end="", flush=True)

    try:
        async with asyncio.timeout(300):
            await rpc(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}})
            env = [
                {"name": name, "value": os.environ[name]}
                for name in ("BUZZ_PRIVATE_KEY", "BUZZ_RELAY_URL", "BUZZ_AUTH_TAG")
                if name in os.environ
            ]
            env.append({"name": "BUZZ_CONTEXT_CHANNEL_ID", "value": channel})
            # Mirror build_mcp_servers: the harness always serializes nsec.
            for entry in env:
                if entry["name"] == "BUZZ_PRIVATE_KEY":
                    entry["value"] = bech32_encode(
                        "nsec", convertbits(parse_secret(entry["value"]), 8, 5)
                    )
            session = await rpc(
                2,
                "session/new",
                {
                    "cwd": str(root / ".runtime"),
                    "systemPrompt": (root / ".runtime/system-prompt.md").read_text(),
                    "mcpServers": [
                        {
                            "name": "documents",
                            "command": str(root / "documents"),
                            "args": [],
                            "env": env,
                        }
                    ],
                },
            )
            print("SESSION ready with document tools only", flush=True)
            await rpc(
                3,
                "session/prompt",
                {
                    "sessionId": session["sessionId"],
                    "prompt": [
                        {
                            "type": "text",
                            "text": f"Event ID: {message}\n이 메시지에 첨부한 작업지시서 전체를 읽고 오더 초안 준비에 필요한 품번, 시즌, 색상, 총수량과 불명확한 항목을 한국어로 요약해줘. 표도 직접 확인해줘. 공통 오더 등록 스킬 절차를 적용하되 이번 검증에서는 ERP 조회/초안 생성/등록은 하지 마.",
                        }
                    ],
                },
            )
            print("\nPASS live attachment agent smoke", flush=True)
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--message", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.channel, args.message))
