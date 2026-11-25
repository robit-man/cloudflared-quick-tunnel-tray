"""Noise_NNpsk0_25519_AESGCM_SHA256 responder for Browser Terminal v2."""

from __future__ import annotations

import asyncio
from hashlib import sha256

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


PROTOCOL = b"Noise_NNpsk0_25519_AESGCM_SHA256"
PROLOGUE = b"tty-websocket-v2"
MAX_MESSAGE = 65535
MAX_PAYLOAD = MAX_MESSAGE - 17  # GCM tag and encrypted message-type byte.
HANDSHAKE_SIZE = 32 + 16
ZERO_NONCE = bytes(12)
RECORD_LIMIT = 2 ** 32


def derive(chaining_key: bytes, material: bytes, count: int) -> tuple[bytes, ...]:
    output = HKDF(hashes.SHA256(), count * 32, chaining_key, b"").derive(material)
    return tuple(output[pos:pos + 32] for pos in range(0, len(output), 32))


def respond(psk: bytes, message: bytes) -> tuple[bytes, AESGCM, AESGCM]:
    # Noise rev. 34, sections 5 and 9. Both handshake payloads are empty.
    if not isinstance(message, bytes) or len(message) != HANDSHAKE_SIZE:
        raise ValueError("Invalid Noise handshake message")
    initial = PROTOCOL.ljust(32, b"\0")
    transcript = sha256(initial + PROLOGUE).digest()
    chain, psk_hash, _ = derive(initial, psk, 3)
    transcript = sha256(transcript + psk_hash).digest()

    client_key, client_tag = message[:32], message[32:]
    transcript = sha256(transcript + client_key).digest()
    chain, handshake_key = derive(chain, client_key, 2)
    AESGCM(handshake_key).decrypt(ZERO_NONCE, client_tag, transcript)
    transcript = sha256(transcript + client_tag).digest()

    ephemeral = X25519PrivateKey.generate()
    public = ephemeral.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    transcript = sha256(transcript + public).digest()
    chain, _ = derive(chain, public, 2)
    chain, handshake_key = derive(chain, ephemeral.exchange(X25519PublicKey.from_public_bytes(client_key)), 2)
    reply = public + AESGCM(handshake_key).encrypt(ZERO_NONCE, b"", transcript)
    client_key, server_key = derive(chain, b"", 2)
    return reply, AESGCM(client_key), AESGCM(server_key)


class SecureTransportError(Exception):
    """The relay delivered an invalid or unauthenticated transport message."""


class SecureSocket:
    def __init__(self, socket, incoming: AESGCM, outgoing: AESGCM) -> None:
        self.socket = socket
        self.incoming = incoming
        self.outgoing = outgoing
        self.read_sequence = 0
        self.write_sequence = 0
        self.write_lock = asyncio.Lock()
        self.request_headers = socket.request_headers

    async def recv(self) -> str | bytes:
        frame = await self.socket.recv()
        if self.read_sequence >= RECORD_LIMIT or not isinstance(frame, bytes) or not 17 <= len(frame) <= MAX_MESSAGE:
            raise SecureTransportError("Invalid Noise frame")
        try:
            body = self.incoming.decrypt(self.read_sequence.to_bytes(12, "big"), frame, None)
            self.read_sequence += 1
            if body[:1] == b"\x00":
                return body[1:].decode("utf-8")
            if body[:1] == b"\x01":
                return body[1:]
        except (InvalidTag, UnicodeError):
            raise SecureTransportError("Invalid Noise frame") from None
        raise SecureTransportError("Unknown Noise message type")

    async def __aiter__(self):
        while True:
            yield await self.recv()

    async def send(self, message: str | bytes) -> None:
        marker, body = (b"\x00", message.encode("utf-8")) if isinstance(message, str) else (b"\x01", message)
        async with self.write_lock:
            for pos in range(0, max(1, len(body)), MAX_PAYLOAD):
                if self.write_sequence >= RECORD_LIMIT:
                    raise SecureTransportError("Noise record limit reached")
                nonce = self.write_sequence.to_bytes(12, "big")
                frame = self.outgoing.encrypt(nonce, marker + body[pos:pos + MAX_PAYLOAD], None)
                self.write_sequence += 1
                await self.socket.send(frame)

    async def close(self, code: int = 1000) -> None:
        await self.socket.close(code)

    async def wait_closed(self) -> None:
        await self.socket.wait_closed()
