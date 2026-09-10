"""Read only signed attachment events in the harness-provided conversation."""

import base64
import hashlib
import json
import os
import re
import time
import uuid
from urllib.parse import urlsplit

import httpx
from bech32 import bech32_decode, convertbits
from coincurve import PrivateKey, PublicKeyXOnly

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024


def parse_secret(secret):
    """Accept Buzz's hex configuration and NIP-19 nsec wire representation."""
    if re.fullmatch(r"[0-9a-fA-F]{64}", secret):
        return bytes.fromhex(secret)
    if len(secret) <= 90:
        prefix, words = bech32_decode(secret)
        if prefix == "nsec" and words is not None:
            octets = convertbits(words, 5, 8, False)
            if octets is not None and len(octets) == 32:
                return bytes(octets)
    raise ValueError(
        "Invalid Buzz private key: expected 32-byte hex or checksum-valid nsec"
    )


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def event_digest(event):
    return hashlib.sha256(
        encoded(
            [
                0,
                event["pubkey"],
                event["created_at"],
                event["kind"],
                event["tags"],
                event["content"],
            ]
        )
    ).digest()


def verify_event(event):
    digest = event_digest(event)
    if digest.hex() != event["id"] or not PublicKeyXOnly(
        bytes.fromhex(event["pubkey"])
    ).verify(bytes.fromhex(event["sig"]), digest):
        raise ValueError("Attachment message signature is invalid")


class RelayDocuments:
    """No arbitrary URL/path API: callers must name an attachment message."""

    def __init__(self, relay, channel, secret, auth_tag=None, transport=None):
        relay = re.sub(r"^ws", "http", relay).rstrip("/")
        origin = urlsplit(relay)
        if origin.scheme not in ("http", "https") or not origin.hostname or origin.path:
            raise ValueError("Configure one relay origin without a path")
        if origin.username or origin.password or origin.query or origin.fragment:
            raise ValueError("Invalid relay origin")
        self.relay = relay
        self.channel = str(uuid.UUID(channel))  # Missing scope fails closed.
        self.key = PrivateKey(parse_secret(secret))
        self.auth_tag = auth_tag
        self.transport = transport

    @classmethod
    def from_env(cls):
        return cls(
            os.environ["BUZZ_RELAY_URL"],
            os.environ["BUZZ_CONTEXT_CHANNEL_ID"],
            os.environ["BUZZ_PRIVATE_KEY"],
            os.environ.get("BUZZ_AUTH_TAG"),
        )

    def request(self, method, url, body=None, limit=MAX_EVENT_BYTES):
        tags = [["u", url], ["method", method], ["nonce", str(uuid.uuid4())]]
        if body is not None:
            tags.append(["payload", hashlib.sha256(body).hexdigest()])
        kind = 27235
        if method == "GET":
            digest = urlsplit(url).path.rsplit("/", 1)[-1].split(".", 1)[0]
            tags = [
                ["t", "get"],
                ["x", digest],
                ["expiration", str(int(time.time()) + 60)],
                ["server", urlsplit(url).netloc],
                ["nonce", str(uuid.uuid4())],
            ]
            kind = 24242
        event = {
            "pubkey": self.key.public_key_xonly.format().hex(),
            "created_at": int(time.time()),
            "kind": kind,
            "tags": tags,
            "content": "Read Buzz attachment" if kind == 24242 else "",
        }
        digest = event_digest(event)
        event.update(id=digest.hex(), sig=self.key.sign_schnorr(digest).hex())
        authorization = (
            base64.urlsafe_b64encode(encoded(event)).decode().rstrip("=")
            if kind == 24242
            else base64.b64encode(encoded(event)).decode()
        )
        headers = {
            "Authorization": "Nostr " + authorization,
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
        }
        if self.auth_tag:
            headers["x-auth-tag"] = self.auth_tag
        deadline = time.monotonic() + 30
        with (
            httpx.Client(
                timeout=10,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client,
            client.stream(method, url, content=body, headers=headers) as response,
        ):
            if response.status_code != 200:
                raise ValueError(f"Relay {method} failed: HTTP {response.status_code}")
            if int(response.headers.get("content-length", "0")) > limit:
                raise ValueError("Attachment exceeds the configured size limit")
            chunks = bytearray()
            for chunk in response.iter_bytes(64 * 1024):
                if len(chunks) + len(chunk) > limit or time.monotonic() > deadline:
                    raise ValueError("Relay response exceeds byte/time limit")
                chunks.extend(chunk)
            return bytes(chunks)

    def attachments(self, message_id):
        if not re.fullmatch(r"[0-9a-f]{64}", message_id):
            raise ValueError(
                "Use the full message event ID from the current conversation"
            )
        query = [{"ids": [message_id], "kinds": [9], "#h": [self.channel], "limit": 1}]
        events = json.loads(self.request("POST", self.relay + "/query", encoded(query)))
        if not isinstance(events, list) or len(events) != 1:
            raise ValueError(
                "Attachment message is not accessible in this conversation"
            )
        event = events[0]
        verify_event(event)
        if (
            event["id"] != message_id
            or event["kind"] != 9
            or ["h", self.channel] not in event["tags"]
        ):
            raise ValueError("Attachment message does not belong to this conversation")
        result = []
        for tag in event["tags"]:
            if not tag or tag[0] != "imeta":
                continue
            fields = dict(item.split(" ", 1) for item in tag[1:] if " " in item)
            url = fields.get("url", "")
            parsed = urlsplit(url)
            # Content-addressed relay media only. No external fetch, redirects,
            # credentials, query strings, path traversal or local-file access.
            match = re.fullmatch(
                r"/media/([0-9a-f]{64})(?:\.[a-zA-Z0-9]{1,10})?", parsed.path
            )
            if (
                parsed.scheme + "://" + parsed.netloc != self.relay
                or not match
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "Attachment must be hosted on the configured relay /media origin"
                )
            digest = match.group(1)
            if fields.get("x", digest) != digest:
                raise ValueError("Attachment hash metadata disagrees with its URL")
            size = int(fields.get("size", "0"))
            if size < 0 or size > MAX_FILE_BYTES:
                raise ValueError("Attachment exceeds 25 MiB limit")
            result.append(
                {
                    "index": len(result),
                    "filename": fields.get("filename", parsed.path.rsplit("/", 1)[-1]),
                    "mime": fields.get("m", ""),
                    "sha256": digest,
                    "size": size,
                    "url": url,
                }
            )
        if len(result) > 20:
            raise ValueError("Too many attachments in one message (maximum 20)")
        return result

    def download(self, message_id, index):
        attachments = self.attachments(
            message_id
        )  # Recheck access, even on later page reads.
        if not 0 <= index < len(attachments):
            raise ValueError("Unknown attachment index; call list_attachments first")
        meta = attachments[index]
        data = self.request("GET", meta["url"], limit=MAX_FILE_BYTES)
        if hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise ValueError("Downloaded attachment failed SHA-256 verification")
        return meta, data
