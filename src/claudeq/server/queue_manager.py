"""
Queue management for ClaudeQ server.

Handles message queue persistence and operations.
"""

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
        self.queue: deque[str] = deque()
        self.recently_sent: list[str] = []
        self._lock = threading.Lock()
        self._recently_sent_lock = threading.Lock()

    def load(self) -> None:
        """Load queue from file."""
        if self.queue_file.exists():
            with open(self.queue_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.queue.append(line)

    def save(self) -> None:
        """Save queue to file."""
        with open(self.queue_file, 'w') as f:
            for msg in self.queue:
                f.write(msg + '\n')

    def add(self, message: str) -> int:
        """
        Add a message to the queue.

        Args:
            message: Message to add.

        Returns:
            Current queue size after adding.
        """
        with self._lock:
            self.queue.append(message)
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
                message = self.queue.popleft()
                self.save()
                return message
            return None

    def peek(self) -> Optional[str]:
        """
        Return the next message without removing it.

        Returns:
            Next message or None if queue is empty.
        """
        with self._lock:
            if self.queue:
                return self.queue[0]
            return None

    def requeue(self, message: str) -> None:
        """
        Put a message back at the front of the queue.

        Args:
            message: Message to requeue.
        """
        with self._lock:
            self.queue.appendleft(message)
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
        Get current queue contents.

        Returns:
            List of queued messages.
        """
        with self._lock:
            return list(self.queue)

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
