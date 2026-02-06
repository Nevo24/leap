"""
Queue management for ClaudeQ server.

Handles message queue persistence and operations.
"""

import random
import string
import threading
from collections import deque
from pathlib import Path
from typing import Optional


class QueueManager:
    """Manages the message queue for a ClaudeQ server."""

    def __init__(self, queue_file: Path, max_recently_sent: int = 20):
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
        if self.queue_file.exists():
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

    def save(self) -> None:
        """Save queue to file."""
        with open(self.queue_file, 'w') as f:
            for entry in self.queue:
                f.write(f"{entry['id']}|{entry['msg']}\n")

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

        Args:
            message: Message that was sent.
        """
        with self._recently_sent_lock:
            self.recently_sent.append(message)
            if len(self.recently_sent) > self.max_recently_sent:
                self.recently_sent.pop(0)

    def get_recently_sent(self) -> list[str]:
        """
        Get list of recently sent messages.

        Returns:
            Copy of the recently sent messages list.
        """
        with self._recently_sent_lock:
            return list(self.recently_sent)

    def get_contents(self) -> list[str]:
        """
        Get current queue contents with IDs.

        Returns:
            List of messages formatted as "<id> message".
        """
        with self._lock:
            return [f"<{entry['id']}> {entry['msg']}" for entry in self.queue]

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
