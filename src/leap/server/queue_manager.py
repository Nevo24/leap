"""
Queue management for Leap server.

Handles message queue persistence and operations.
"""

import logging
import os
import random
import string
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class QueueManager:
    """Manages the message queue for a Leap server."""

    def __init__(self, queue_file: Path, max_recently_sent: int = 20) -> None:
        """
        Initialize queue manager.

        Args:
            queue_file: Path to the queue persistence file.
            max_recently_sent: Maximum number of recently sent messages to track.
        """
        self.queue_file = queue_file
        self.max_recently_sent = max_recently_sent
        self.queue: deque[dict[str, str]] = deque()  # Each entry: {'id': ..., 'msg': ...}
        self.recently_sent: list[str] = []
        self.total_sent: int = 0
        self._last_sent_time: float = 0.0
        self._lock = threading.Lock()
        self._recently_sent_lock = threading.Lock()

    def _generate_id(self) -> str:
        """
        Generate a short unique ID for a message.

        Returns:
            6-character alphanumeric ID.
        """
        return ''.join(random.choices(string.ascii_letters + string.digits, k=6))

    def load(self) -> None:
        """Load queue from file."""
        if not self.queue_file.exists():
            return
        try:
            with open(self.queue_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    # Handle both old format (plain text) and new format (id|message)
                    if '|' in line:
                        parts = line.split('|', 1)
                        msg_id = parts[0]
                        message = parts[1] if len(parts) > 1 else ''
                    else:
                        # Old format - migrate by generating new ID
                        msg_id = self._generate_id()
                        message = line

                    self.queue.append({'id': msg_id, 'msg': message})
        except OSError:
            logger.warning("Failed to load queue from %s", self.queue_file, exc_info=True)

    def save(self) -> None:
        """Save queue to file atomically (write to temp + rename)."""
        try:
            dir_path = self.queue_file.parent
            fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix='.tmp')
            try:
                with os.fdopen(fd, 'w') as f:
                    for entry in self.queue:
                        f.write(f"{entry['id']}|{entry['msg']}\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(self.queue_file))
            except BaseException:
                # Clean up temp file on any failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError:
            logger.warning("Failed to persist queue to %s", self.queue_file, exc_info=True)

    def add(self, message: str) -> int:
        """
        Add a message to the queue.

        Args:
            message: Message to add.

        Returns:
            Current queue size after adding.
        """
        with self._lock:
            msg_id = self._generate_id()
            self.queue.append({'id': msg_id, 'msg': message})
            self.save()
            return len(self.queue)

    def prepend(self, messages: list[str]) -> int:
        """Insert messages at the front of the queue, preserving their order.

        Args:
            messages: Messages to prepend (first element will be at the front).

        Returns:
            Current queue size after prepending.
        """
        with self._lock:
            for msg in reversed(messages):
                msg_id = self._generate_id()
                self.queue.appendleft({'id': msg_id, 'msg': msg})
            self.save()
            return len(self.queue)

    def pop(self) -> Optional[str]:
        """
        Remove and return the next message from the queue.

        Returns:
            Next message or None if queue is empty.
        """
        with self._lock:
            if self.queue:
                entry = self.queue.popleft()
                self.save()
                return entry['msg']
            return None

    def peek(self) -> Optional[str]:
        """
        Return the next message without removing it.

        Returns:
            Next message or None if queue is empty.
        """
        with self._lock:
            if self.queue:
                return self.queue[0]['msg']
            return None

    def requeue(self, message: str) -> None:
        """
        Put a message back at the front of the queue.

        Args:
            message: Message to requeue.
        """
        with self._lock:
            # Requeue with a new ID
            msg_id = self._generate_id()
            self.queue.appendleft({'id': msg_id, 'msg': message})
            self.save()

    def track_sent(self, message: str) -> None:
        """
        Track a sent message for client notifications.

        Messages arriving within 0.5s of the previous one are combined
        (appended with a newline).  This merges pasted multi-line text
        that terminals without bracketed-paste support send as rapid
        individual lines.

        Args:
            message: Message that was sent.
        """
        with self._recently_sent_lock:
            now = time.monotonic()
            if (self.recently_sent
                    and now - self._last_sent_time < 0.5):
                # Rapid follow-up — combine with previous message
                self.recently_sent[-1] += '\n' + message
            else:
                self.recently_sent.append(message)
                if len(self.recently_sent) > self.max_recently_sent:
                    self.recently_sent.pop(0)
            self.total_sent += 1
            self._last_sent_time = now

    def get_recently_sent(self) -> tuple[list[str], int]:
        """
        Get list of recently sent messages and total sent count.

        Returns both under the same lock to ensure consistency
        (the total count always matches the list snapshot).

        Returns:
            Tuple of (recently sent messages list copy, total sent count).
        """
        with self._recently_sent_lock:
            return list(self.recently_sent), self.total_sent

    def get_contents(self) -> list[str]:
        """
        Get current queue contents with IDs.

        Returns:
            List of messages formatted as "<id> message".
        """
        with self._lock:
            return [f"<{entry['id']}> {entry['msg']}" for entry in self.queue]

    def get_details(self) -> list[dict[str, str]]:
        """
        Get current queue entries as a list of dicts.

        Returns:
            List of {'id': ..., 'msg': ...} dictionaries (copies).
        """
        with self._lock:
            return [dict(entry) for entry in self.queue]

    def get_message_by_index(self, index: int) -> Optional[dict[str, str]]:
        """
        Get message at a specific index for editing.

        Args:
            index: Queue index (0-based).

        Returns:
            Dictionary with 'id' and 'msg' keys, or None if index invalid.
        """
        with self._lock:
            if 0 <= index < len(self.queue):
                return dict(self.queue[index])  # Return a copy
            return None

    def reorder_by_ids(self, ordered_ids: list[str]) -> bool:
        """
        Reorder the queue to match the given ID order.

        IDs not found in the queue are silently skipped. Queue entries
        whose IDs are not in ``ordered_ids`` are appended at the end.

        Args:
            ordered_ids: Message IDs in the desired order.

        Returns:
            True if the queue was reordered successfully.
        """
        with self._lock:
            by_id = {entry['id']: entry for entry in self.queue}
            new_order: list[dict[str, str]] = []
            seen: set[str] = set()
            for mid in ordered_ids:
                if mid in by_id and mid not in seen:
                    new_order.append(by_id[mid])
                    seen.add(mid)
            # Append any entries not mentioned (safety net)
            for entry in self.queue:
                if entry['id'] not in seen:
                    new_order.append(entry)
            self.queue = deque(new_order)
            self.save()
            return True

    def edit_message_by_id(self, msg_id: str, new_message: str) -> bool:
        """
        Edit a message by its ID.

        Args:
            msg_id: Message ID to edit.
            new_message: New message content.

        Returns:
            True if message was found and edited, False otherwise.
        """
        with self._lock:
            for entry in self.queue:
                if entry['id'] == msg_id:
                    entry['msg'] = new_message
                    self.save()
                    return True
            return False

    @property
    def size(self) -> int:
        """Get current queue size."""
        with self._lock:
            return len(self.queue)

    @property
    def is_empty(self) -> bool:
        """Check if queue is empty."""
        with self._lock:
            return len(self.queue) == 0

    def clear(self) -> None:
        """Clear all messages from the queue."""
        with self._lock:
            self.queue.clear()
            self.save()

    def delete_file_if_empty(self) -> None:
        """Delete the queue file if the queue is empty.

        Holds the internal lock so no message can be added between the
        emptiness check and the unlink.
        """
        with self._lock:
            if not self.queue and self.queue_file.exists():
                try:
                    self.queue_file.unlink()
                except OSError:
                    pass
