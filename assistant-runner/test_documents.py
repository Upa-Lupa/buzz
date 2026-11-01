"""Production-seam tests: signed relay messages, parser subprocess, MCP and shared skills."""

import asyncio
import base64
import hashlib
import io
import json
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
from bech32 import bech32_encode, convertbits
from coincurve import PrivateKey
from docx import Document
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openpyxl import Workbook
from PIL import Image

from deploy_skills import deploy
from document_parser import checked_zip, parse
from documents_mcp import parse_isolated
from relay_documents import (
    RelayDocuments,
    encoded,
    event_digest,
    parse_secret,
    verify_event,
)


def png():
    out = io.BytesIO()
    Image.new("RGB", (80, 40), "red").save(out, "PNG")
    return out.getvalue()


class DocumentsTest(unittest.TestCase):
    def test_legacy_excel_stored_values(self):
        import xlwt

        book = xlwt.Workbook()
        sheet = book.add_sheet("오더")
        sheet.write(0, 0, "MKETL3Z12")
        sheet.write(0, 1, 700)
        out = io.BytesIO()
        book.save(out)
        result = parse_isolated(out.getvalue(), "legacy.xls", "read", 1, 0)
        self.assertIn("MKETL3Z12", result["text"])
        self.assertIn("700", result["text"])

    def test_scanned_pdf_and_image_pixels(self):
        image = Image.open(io.BytesIO(png()))
        pdf = io.BytesIO()
        image.save(pdf, "PDF")
        for data, name in [(png(), "test.png"), (pdf.getvalue(), "scan.pdf")]:
            manifest = parse_isolated(data, name, "inspect", 1, 0)
            self.assertEqual(len(manifest["parts"]), 1)
            result = parse_isolated(data, name, "view", 1, 0)
            loaded = Image.open(io.BytesIO(base64.b64decode(result["image"])))
            self.assertGreater(loaded.width, 0)
        self.assertEqual(
            parse_isolated(pdf.getvalue(), "scan.pdf", "read", 1, 0)["text"], ""
        )

    def test_word_tables_embedded_image_and_pagination(self):
        doc = Document()
        doc.add_paragraph("품번 MKETL3Z12 " + "가" * 8500)
        table = doc.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "컬러"
        table.cell(0, 1).text = "블랙"
        doc.add_picture(io.BytesIO(png()))
        out = io.BytesIO()
        doc.save(out)
        data = out.getvalue()
        parts = parse_isolated(data, "tech.docx", "inspect", 1, 0)["parts"]
        self.assertTrue(parts[1]["viewable"])
        result = parse_isolated(data, "tech.docx", "read", 1, 0)
        self.assertEqual(result["next_offset"], 8000)
        remaining = parse_isolated(data, "tech.docx", "read", 1, result["next_offset"])
        self.assertIn("컬러 | 블랙", remaining["text"])
        self.assertIsNone(remaining["next_offset"])
        self.assertIn("image", parse_isolated(data, "tech.docx", "view", 2, 0))

    def test_excel_all_sheets_coordinates_formulas(self):
        book = Workbook()
        sheet = book.active
        sheet.title = "오더"
        sheet.append(["품번", "수량"])
        sheet.append(["MKETL3Z12", 300])
        sheet["C2"] = "=B2*2"
        sheet["A101"] = "마지막"
        hidden = book.create_sheet("참고")
        hidden.sheet_state = "hidden"
        hidden["A1"] = "비고"
        out = io.BytesIO()
        book.save(out)
        data = out.getvalue()
        self.assertEqual(len(parse(data, "test.xlsx")["parts"]), 3)
        text = parse_isolated(data, "test.xlsx", "read", 1, 0)["text"]
        self.assertIn("A2=MKETL3Z12", text)
        self.assertIn("B2=300", text)
        self.assertIn("not recalculated", text)
        self.assertIn("A101=마지막", parse(data, "test.xlsx", "read", 2)["text"])
        self.assertIn("hidden", parse(data, "test.xlsx")["parts"][2]["label"])

    def test_invalid_documents_offsets_and_zip_bombs(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            parse_isolated(b"old word", "legacy.doc", "inspect", 1, 0)
        with self.assertRaises(ValueError):
            parse_isolated(b"invalid", "broken.xlsx", "inspect", 1, 0)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("huge.xml", b"0" * (26 * 1024 * 1024))
        with self.assertRaises(ValueError):
            checked_zip(out.getvalue())
        with self.assertRaises(ValueError):
            parse(png(), "image.png", "view", 0)

    def test_parser_timeout_propagates(self):
        import subprocess

        with (
            patch(
                "documents_mcp.subprocess.run",
                side_effect=subprocess.TimeoutExpired("parser", 25),
            ),
            self.assertRaisesRegex(ValueError, "timed out"),
        ):
            parse_isolated(png(), "image.png", "view", 1, 0)

    def test_shared_skill_deployment_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "erp/skills/order-registration/SKILL.md"
            source.parent.mkdir(parents=True)
            content = b"---\nname: order-registration\ndescription: Draft orders\n---\nApproval required.\n"
            source.write_bytes(content)
            manifest = deploy(root / "erp", root / "runtime")
            deployed = root / "runtime/.agents/skills/order-registration/SKILL.md"
            self.assertEqual(deployed.read_bytes(), content)
            prompt = (root / "runtime/system-prompt.md").read_text()
            self.assertIn(content.decode(), prompt)
            self.assertIn("user_facing_response_contract", prompt)
            self.assertIn("빠진 필수 항목", prompt)
            self.assertIn("comma-separated", prompt)
            self.assertIn("only allowed headings", prompt)
            self.assertNotIn("확인한 정보\n- <field name>", prompt)
            self.assertIn("get_*_registration_requirements", prompt)
            self.assertIn("resolve_registration_references", prompt)
            self.assertGreater(
                prompt.index("user_facing_response_contract"),
                prompt.index("deployed_shared_skill"),
            )
            self.assertEqual(
                manifest["order-registration"]["sha256"],
                hashlib.sha256(content).hexdigest(),
            )

    def test_mcp_stdio_catalog_and_closed_unscoped_access(self):
        async def scenario():
            import sys

            params = StdioServerParameters(
                command=sys.executable,
                args=[str(Path(__file__).with_name("documents_mcp.py"))],
                env={
                    "BUZZ_RELAY_URL": "http://localhost:3030",
                    "BUZZ_PRIVATE_KEY": "01" * 32,
                    "BUZZ_CONTEXT_CHANNEL_ID": "",
                },
            )
            async with (
                stdio_client(params) as (reader, writer),
                ClientSession(reader, writer) as session,
            ):
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                self.assertEqual(
                    names,
                    {
                        "list_attachments",
                        "inspect_attachment",
                        "read_attachment",
                        "view_attachment",
                    },
                )
                result = await session.call_tool(
                    "list_attachments", {"message_id": "a" * 64}
                )
                self.assertTrue(result.isError)

        asyncio.run(scenario())


class RelayBoundaryTest(unittest.TestCase):
    def test_harness_nsec_and_hex_produce_same_identity(self):
        secret = bytes(range(1, 33))
        nsec = bech32_encode("nsec", convertbits(secret, 8, 5))
        with patch.dict(
            "os.environ",
            {
                "BUZZ_RELAY_URL": "ws://localhost:3030",
                "BUZZ_CONTEXT_CHANNEL_ID": self.channel,
                "BUZZ_PRIVATE_KEY": nsec,
            },
        ):
            actual = RelayDocuments.from_env()
        self.assertEqual(actual.key.secret, secret)
        self.assertEqual(parse_secret(secret.hex()), secret)
        for invalid in [
            nsec[:-1] + ("q" if nsec[-1] != "q" else "p"),
            bech32_encode("npub", convertbits(secret, 8, 5)),
            bech32_encode("nsec", convertbits(secret[:-1], 8, 5)),
            "bad-key",
        ]:
            with self.assertRaisesRegex(ValueError, "Invalid Buzz private key"):
                parse_secret(invalid)

    def setUp(self):
        self.channel = str(uuid.uuid4())
        self.key = PrivateKey()
        self.data = png()
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.url = f"http://localhost:3030/media/{self.digest}.png"

    def event(self, url=None, channel=None):
        event = {
            "pubkey": self.key.public_key_xonly.format().hex(),
            "created_at": 1,
            "kind": 9,
            "content": "test",
            "tags": [
                ["h", channel or self.channel],
                [
                    "imeta",
                    "url " + (url or self.url),
                    "m image/png",
                    "filename test.png",
                ],
            ],
        }
        digest = event_digest(event)
        event.update(id=digest.hex(), sig=self.key.sign_schnorr(digest).hex())
        return event

    def client(self, event, media=None, redirect=False):
        def handle(request):
            raw_auth = request.headers["Authorization"].removeprefix("Nostr ")
            auth = json.loads(
                base64.urlsafe_b64decode(raw_auth + "=" * (-len(raw_auth) % 4))
            )
            verify_event(auth)
            if request.method == "POST":
                self.assertEqual(auth["kind"], 27235)
                query = json.loads(request.content)[0]
                self.assertEqual(query["#h"], [self.channel])
                self.assertEqual(query["kinds"], [9])
                return httpx.Response(200, content=encoded([event]))
            self.assertEqual(str(request.url), self.url)
            self.assertEqual(auth["kind"], 24242)
            self.assertTrue(auth["content"].strip())
            self.assertIn(["t", "get"], auth["tags"])
            self.assertIn(["x", self.digest], auth["tags"])
            if redirect:
                return httpx.Response(
                    302, headers={"Location": "http://other.invalid/secret"}
                )
            return httpx.Response(
                200, content=media if media is not None else self.data
            )

        return RelayDocuments(
            "ws://localhost:3030",
            self.channel,
            bech32_encode("nsec", convertbits(self.key.secret, 8, 5)),
            transport=httpx.MockTransport(handle),
        )

    def test_signed_message_and_hash_verified_download(self):
        event = self.event()
        meta, data = self.client(event).download(event["id"], 0)
        self.assertEqual(data, self.data)
        self.assertEqual(meta["sha256"], self.digest)

    def test_other_dm_and_forged_event_rejected(self):
        other = self.event(channel=str(uuid.uuid4()))
        with self.assertRaisesRegex(ValueError, "conversation"):
            self.client(other).download(other["id"], 0)
        forged = self.event()
        forged["content"] = "tampered"
        with self.assertRaisesRegex(ValueError, "signature"):
            self.client(forged).download(forged["id"], 0)

    def test_external_urls_traversal_redirects_and_hash_mismatch_rejected(self):
        for url in [
            "file:///etc/passwd",
            "http://127.0.0.1:3030/media/" + self.digest,
            "http://localhost:3030/media/../secret",
            self.url + "?redirect=1",
        ]:
            event = self.event(url=url)
            with self.assertRaises(ValueError):
                self.client(event).download(event["id"], 0)
        event = self.event()
        with self.assertRaisesRegex(ValueError, "302"):
            self.client(event, redirect=True).download(event["id"], 0)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.client(event, media=b"tampered").download(event["id"], 0)


if __name__ == "__main__":
    unittest.main()
