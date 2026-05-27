#!/usr/bin/python3

"""sources.aisstream — aisstream.io WebSocket source.

Subscribes to wss://stream.aisstream.io/v0/stream and forwards every
received message as a normalized record on the shared queue.  Handles
reconnect/backoff, re-subscribes on every (re)connect, and never blocks
the receive loop on indexer back-pressure (drop-oldest in BaseAISSource).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import ssl
import threading
import time
from typing import Dict, List, Optional

import websockets  # pyright: ignore[reportMissingImports]

from .base import BaseAISSource

AIS_STREAM_URL = "wss://stream.aisstream.io/v0/stream"
MAX_BACKOFF_S = 60.0
# Library-level WebSocket keep-alive cadence.  aisstream.io documents no
# client-required heartbeat, but a configured ping keeps NAT mappings alive
# and surfaces dead-half-open connections within ~40s.
PING_INTERVAL_S = 20.0
PING_TIMEOUT_S = 20.0


class FatalAISStreamError(Exception):
    """Raised when the server reports an unrecoverable error (e.g. invalid
    API key).  The runner exits without reconnecting — retrying would just
    spin against the same permanent failure."""


class AISStreamIOSource(BaseAISSource):
    """One thread; runs an asyncio loop internally for the websocket."""

    def __init__(self, *, name: str, out_queue, stop_event: threading.Event,
                 api_key: str, bounding_boxes: List[List[List[float]]],
                 mmsi_filter: List[str] | None = None,
                 message_type_filter: List[str] | None = None,
                 feed_id: str = "aisstream.io",
                 insecure: bool = False):
        super().__init__(name=name, out_queue=out_queue, stop_event=stop_event)
        self._api_key = api_key
        self._bboxes = bounding_boxes
        self._mmsi_filter = list(mmsi_filter or [])
        self._type_filter = list(message_type_filter or [])
        self._feed_id = feed_id
        # aisstream.io has historically let their LetsEncrypt cert lapse on
        # renewal day.  insecure=True builds an SSL context that skips cert
        # validation so ingest keeps flowing while they renew.  Should NOT be
        # the long-term default — flag it visibly at startup.
        self._insecure = bool(insecure)
        self._ssl_ctx: Optional[ssl.SSLContext] = None
        if self._insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl_ctx = ctx

    def source_metadata(self) -> Dict:
        return {
            "kind": "aisstream",
            "vendor": "aisstream.io",
            "module": "ais",
            "dataset": "ais.aisstream",
            "feed_id": self._feed_id,
        }

    def _subscribe_payload(self) -> Dict:
        payload: Dict = {
            "APIKey": self._api_key,
            "BoundingBoxes": self._bboxes,
        }
        if self._mmsi_filter:
            payload["FiltersShipMMSI"] = self._mmsi_filter
        if self._type_filter:
            payload["FilterMessageTypes"] = self._type_filter
        return payload

    async def _connect_and_consume(self):
        # Bound the TLS handshake so a hung peer doesn't pin the source
        # thread indefinitely.  ping_interval/ping_timeout drive library-
        # level keep-alive; aisstream.io documents no client heartbeat, but
        # the WS-level ping catches half-open connections within ~40s.
        connect_kwargs: Dict = dict(
            ping_interval=PING_INTERVAL_S,
            ping_timeout=PING_TIMEOUT_S,
            close_timeout=5.0,
        )
        if self._ssl_ctx is not None:
            connect_kwargs["ssl"] = self._ssl_ctx
        ws = await asyncio.wait_for(
            websockets.connect(AIS_STREAM_URL, **connect_kwargs),
            timeout=30.0)
        async with ws:
            await ws.send(json.dumps(self._subscribe_payload()))
            tls_note = " [TLS verification DISABLED]" if self._insecure else ""
            self.log.info("subscribed to %s%s for %d bbox(es), %d mmsi filter(s), %d msg-type filter(s)",
                          AIS_STREAM_URL, tls_note, len(self._bboxes),
                          len(self._mmsi_filter), len(self._type_filter))
            while not self.stop_event.is_set():
                try:
                    # The 60s app-level recv timeout is a belt over the
                    # library's ping (~40s) — if pings somehow succeed but no
                    # AIS frames arrive (subscription went silent), we still
                    # tear down and reconnect rather than wait forever.
                    raw = await asyncio.wait_for(ws.recv(), timeout=60.0)
                except asyncio.TimeoutError:
                    self.log.warning("no data for 60s; reconnecting")
                    return
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                # aisstream.io reports authentication / subscription errors
                # as inline JSON messages, not WebSocket close frames.
                # Treat anything with an "error" field and no MessageType as
                # a server-side problem rather than an AIS record.
                if isinstance(msg, dict) and "error" in msg and "MessageType" not in msg:
                    err = msg.get("error")
                    self.log.error("aisstream.io error message: %s", err)
                    if "api key" in str(err).lower():
                        raise FatalAISStreamError(str(err))
                    continue
                self.enqueue(msg, datetime.datetime.now(datetime.timezone.utc))

    async def _runner(self):
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                await self._connect_and_consume()
                if self.stop_event.is_set():
                    return
                self.log.info("connection closed cleanly; reconnecting")
                backoff = 1.0  # reset after a clean session
            except FatalAISStreamError as exc:
                # API key invalid or similar — retrying will just spin.
                # Signal global shutdown so main() exits cleanly.
                self.log.error("fatal aisstream error; will not reconnect: %s",
                               exc)
                self.stop_event.set()
                return
            except websockets.exceptions.ConnectionClosed as exc:
                self.log.warning(
                    "connection closed (code=%s reason=%r); backoff %.1fs",
                    getattr(exc, "code", "?"), getattr(exc, "reason", ""),
                    backoff)
            except asyncio.TimeoutError:
                # asyncio.TimeoutError is OSError in 3.11+, so it must
                # precede the OSError catch.
                self.log.warning("connect timeout; backoff %.1fs", backoff)
            except (websockets.exceptions.WebSocketException, OSError) as exc:
                self.log.warning("websocket error: %s; backoff %.1fs",
                                 exc, backoff)
            except Exception as exc:
                self.log.exception("unexpected source error: %s", exc)
            if self.stop_event.is_set():
                return
            sleep_s = backoff
            backoff = min(backoff * 2, MAX_BACKOFF_S)
            # Cooperative sleep that wakes early on shutdown
            deadline = time.monotonic() + sleep_s
            while not self.stop_event.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.5)

    def run_loop(self):
        asyncio.run(self._runner())
