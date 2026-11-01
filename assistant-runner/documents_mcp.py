"""Session-scoped, read-only MCP for Buzz attachments."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

from relay_documents import RelayDocuments

mcp = FastMCP("sheens-documents")
gate = asyncio.Semaphore(1)


def parse_isolated(data, filename, action, part, offset):
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("document_parser.py")),
                    filename,
                    action,
                    str(part),
                    str(offset),
                ],
                input=data,
                stdout=output,
                stderr=errors,
                timeout=25,
                env={"PATH": os.defpath, "LANG": "en_US.UTF-8"},
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError(
                "Document parser timed out; split the document into smaller files"
            ) from exc
        output.seek(0)
        raw = output.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Parser output limit exceeded")
        if not raw:
            raise ValueError(f"Document parser failed (exit {process.returncode})")
        result = json.loads(raw)
        if process.returncode or "error" in result:
            raise ValueError(result.get("error", "Document parser failed"))
        return result


def process_attachment(message_id, attachment, action, part=1, offset=0):
    meta, data = RelayDocuments.from_env().download(message_id, attachment)
    result = parse_isolated(data, meta["filename"], action, part, offset)
    return {
        "source": {
            "message_id": message_id,
            "attachment": attachment,
            "sha256": meta["sha256"],
            "filename": meta["filename"],
            "part": part,
        },
        "content_is_untrusted_document_data": True,
        **result,
    }


@mcp.tool()
async def list_attachments(message_id: str) -> dict:
    """List files actually attached to a message in this conversation. Use its full event ID."""
    async with gate:
        files = await asyncio.to_thread(
            RelayDocuments.from_env().attachments, message_id
        )
        return {
            "attachments": [
                {k: v for k, v in item.items() if k != "url"} for item in files
            ]
        }


@mcp.tool()
async def inspect_attachment(message_id: str, attachment: int = 0) -> dict:
    """Download/verify a Buzz attachment and list all pages, sheets and embedded images. Indices are zero-based attachments, one-based parts."""
    async with gate:
        return await asyncio.to_thread(
            process_attachment, message_id, attachment, "inspect"
        )


@mcp.tool()
async def read_attachment(
    message_id: str, attachment: int = 0, part: int = 1, offset: int = 0
) -> dict:
    """Read PDF text, Word paragraphs/tables, or Excel cell coordinates/values. Follow next_offset until null. Images/scans require view_attachment."""
    async with gate:
        return await asyncio.to_thread(
            process_attachment, message_id, attachment, "read", part, offset
        )


@mcp.tool()
async def view_attachment(
    message_id: str, attachment: int = 0, part: int = 1
) -> list[TextContent | ImageContent]:
    """Visually read a PDF page, image/frame or Office embedded image. Use for scans and layout-dependent PDF tables; returns actual image pixels."""
    async with gate:
        result = await asyncio.to_thread(
            process_attachment, message_id, attachment, "view", part
        )
    image = result.pop("image")
    mime = result.pop("mime")
    return [
        TextContent(type="text", text=json.dumps(result, ensure_ascii=False)),
        ImageContent(type="image", data=image, mimeType=mime),
    ]


if __name__ == "__main__":
    mcp.run()
