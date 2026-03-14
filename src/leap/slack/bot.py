"""
SlackBot — main Slack integration daemon for Leap.

Connects to Slack via Socket Mode (outbound WebSocket, no public URL
needed), posts Claude's output to per-session DM threads, and routes
user replies back to Leap sessions.

Usage:
    Run ``leap --slack`` (or ``python -m leap.slack.bot``) to start.
"""

import logging
import os
import sys
from typing import Any, Optional

try:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    from slack_sdk import WebClient
except ImportError:
    print(
        "Slack dependencies not installed.\n"
        "Run: make install-slack-app\n"
    )
    sys.exit(1)

from leap.slack.config import load_slack_config
from leap.slack.message_router import MessageRouter
from leap.slack.output_watcher import OutputWatcher

logger = logging.getLogger(__name__)

_ICON_URL = (
    'https://raw.githubusercontent.com/nevo24/leap'
    '/main/assets/leap-icon.png'
)


class SlackBot:
    """Main Slack bot class.

    Connects via Socket Mode, listens for DM replies, and runs the
    OutputWatcher to post Claude's output to Slack threads.
    """

    def __init__(self) -> None:
        config = load_slack_config()
        if not config:
            print(
                "Slack app not configured.\n"
                "Run: make install-slack-app\n"
            )
            sys.exit(1)

        self._bot_token = config['bot_token']
        self._app_token = config['app_token']
        self._user_id = config['user_id']
        self._dm_channel_id = config['dm_channel_id']

        self._app = App(token=self._bot_token)
        self._client = WebClient(token=self._bot_token)
        self._router = MessageRouter()

        # Register event handlers
        self._app.event("message")(self._handle_message)

        # Output watcher posts Claude output to Slack
        self._watcher = OutputWatcher(
            post_fn=self._post_message,
            channel_id=self._dm_channel_id,
        )

    def start(self) -> None:
        """Start the bot (blocking)."""
        print("Starting Leap Slack bot...")
        print(f"  DM channel: {self._dm_channel_id}")
        print(f"  User: {self._user_id}")
        print()

        self._watcher.start()

        try:
            handler = SocketModeHandler(self._app, self._app_token)
            print("Connected to Slack. Listening for messages...")
            print("Press Ctrl+C to stop.\n")
            handler.start()
        except KeyboardInterrupt:
            print("\nShutting down...")
        finally:
            self._watcher.stop()

    def _handle_message(self, event: dict[str, Any], say: object) -> None:
        """Handle incoming DM messages from the user.

        Only processes messages that:
        - Are from the configured user
        - Are in the configured DM channel
        - Are thread replies (have a thread_ts)
        - Are not bot messages

        Args:
            event: Slack message event.
            say: Slack say function (unused — we post via WebClient).
        """
        # Ignore bot messages
        if event.get('bot_id') or event.get('subtype'):
            return

        # Only respond to the configured user
        if event.get('user') != self._user_id:
            return

        # Only respond in the DM channel
        if event.get('channel') != self._dm_channel_id:
            return

        # Must be a thread reply
        thread_ts = event.get('thread_ts')
        if not thread_ts:
            return

        text = event.get('text', '').strip()
        if not text:
            return

        result = self._router.route_message(thread_ts, text)

        if result == 'queued':
            self._react(event, 'inbox_tray')
        elif result == 'sent':
            self._react(event, 'zap')
        elif result == 'type_text_instead':
            self._post_message(
                self._dm_channel_id,
                ':pencil2: That option is "Type something." — '
                'reply with your answer as text instead.',
                thread_ts,
            )
        elif result == 'invalid_permission':
            self._post_message(
                self._dm_channel_id,
                ':no_entry: Reply with a number to select an option '
                '(e.g. `1`, `2`, `3`).',
                thread_ts,
            )
        elif result == 'offline':
            self._post_message(
                self._dm_channel_id,
                ':red_circle: Session is offline.',
                thread_ts,
            )
        elif result == 'no_session':
            self._post_message(
                self._dm_channel_id,
                ':question: No Leap session found for this thread.',
                thread_ts,
            )

    def _post_message(
        self,
        channel: str,
        text: str,
        thread_ts: Optional[str] = None,
    ) -> Optional[str]:
        """Post a message to Slack.

        Args:
            channel: Channel ID.
            text: Message text (supports mrkdwn).
            thread_ts: Thread timestamp (for thread replies).

        Returns:
            The message timestamp (thread_ts for new threads).
        """
        try:
            kwargs = {
                'channel': channel,
                'text': text,
                'mrkdwn': True,
                'icon_url': _ICON_URL,
            }
            if thread_ts:
                kwargs['thread_ts'] = thread_ts
            response = self._client.chat_postMessage(**kwargs)
            return response.get('ts')
        except Exception:
            logger.exception('Failed to post Slack message')
            return None

    def _react(self, event: dict[str, Any], emoji: str) -> None:
        """Add an emoji reaction to a message.

        Args:
            event: Original message event.
            emoji: Emoji name (without colons).
        """
        try:
            self._client.reactions_add(
                channel=event['channel'],
                name=emoji,
                timestamp=event['ts'],
            )
        except Exception:
            pass  # Non-critical


def _check_singleton() -> None:
    """Ensure only one Slack bot instance runs at a time.

    Uses a PID file to detect stale locks.  If another bot is alive,
    exit.  If the PID file points to a dead process, take over.
    """
    import subprocess
    from leap.utils.constants import SLACK_DIR
    pid_file = SLACK_DIR / "slack-bot.pid"
    SLACK_DIR.mkdir(parents=True, exist_ok=True)

    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            # Check if the process is still alive AND is a Slack bot
            os.kill(old_pid, 0)
            # Verify it's actually a leap-slack process (PID could be recycled)
            result = subprocess.run(
                ['ps', '-o', 'command=', '-p', str(old_pid)],
                capture_output=True, text=True, timeout=2,
            )
            if 'leap-slack' in result.stdout:
                print(
                    f"Another Slack bot is already running (PID {old_pid}).\n"
                    "Kill it first or use the monitor to stop/start.",
                    file=sys.stderr,
                )
                sys.exit(1)
            # PID alive but not a Slack bot — stale PID file
        except (ValueError, OSError):
            pass

    pid_file.write_text(str(os.getpid()))

    import atexit
    atexit.register(lambda: pid_file.unlink(missing_ok=True))


def main() -> None:
    """Entry point for the leap-slack command."""
    _check_singleton()
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    bot = SlackBot()
    bot.start()


if __name__ == '__main__':
    main()
