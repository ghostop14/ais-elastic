#!/usr/bin/python3

"""sources.base — Abstract base class for AIS message sources.

Each source runs as a thread that reads from its transport (WebSocket,
NMEA TCP, file replay, etc.) and pushes normalized record dicts into a
shared bounded queue.  The ECS builder is source-agnostic and consumes
records produced by any concrete source.

Normalized record contract (the dict put on the queue):

    {
        "received_at":     datetime (UTC-aware) — when WE saw it,
        "source_metadata": {
            "kind":    str,   # 'aisstream', 'nmea', etc.
            "vendor":  str,
            "module":  str,   # ECS event.module
            "dataset": str,   # ECS event.dataset
            "feed_id": str,
        },
        "raw":             dict — original upstream message envelope,
    }
"""

from __future__ import annotations

import logging
import queue
import threading
from abc import ABC, abstractmethod
from typing import Dict


class BaseAISSource(ABC, threading.Thread):
    """Abstract source thread.  Subclasses implement :meth:`run_loop`."""

    def __init__(self, name: str, out_queue: "queue.Queue",
                 stop_event: threading.Event):
        threading.Thread.__init__(self, name=name, daemon=True)
        self.out_queue = out_queue
        self.stop_event = stop_event
        self.log = logging.getLogger(f"ais_elastic.source.{name}")
        # Metrics
        self.messages_received = 0
        self.messages_dropped = 0  # queue overflow

    @abstractmethod
    def source_metadata(self) -> Dict:
        """Static descriptor describing the feed.

        See module docstring for required keys (kind/vendor/module/dataset/
        feed_id)."""

    @abstractmethod
    def run_loop(self):
        """Source-specific receive loop.  Should honor self.stop_event."""

    def run(self):
        try:
            self.run_loop()
        except Exception:
            self.log.exception("source thread crashed")

    def enqueue(self, raw_envelope, received_at):
        """Push a normalized record onto the queue.  Drop-oldest on overflow."""
        record = {
            "received_at": received_at,
            "source_metadata": self.source_metadata(),
            "raw": raw_envelope,
        }
        try:
            self.out_queue.put_nowait(record)
            self.messages_received += 1
        except queue.Full:
            # Drop oldest then enqueue; we'd rather lose history than
            # disconnect from the upstream.
            try:
                self.out_queue.get_nowait()
                self.out_queue.put_nowait(record)
            except (queue.Empty, queue.Full):
                pass
            self.messages_dropped += 1
