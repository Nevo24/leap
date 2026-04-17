"""
Leap main entry point.

Thin dispatcher that routes to the appropriate component based on arguments.
"""

import sys

from leap.client import LeapClient
from leap.server import LeapServer
from leap.utils.constants import SOCKET_DIR


def main() -> None:
    """
    Main entry point for the 'leap' command.

    Routes based on arguments:
    - No tag: Shows usage
    - Tag only + server not running: Starts server
    - Tag only + server running: Starts client
    - Tag + message: Queues message via client
    """
    if len(sys.argv) < 2 or sys.argv[1].startswith('-'):
        _show_usage()
        sys.exit(1)

    tag = sys.argv[1]

    socket_path = SOCKET_DIR / f"{tag}.sock"

    if not socket_path.exists():
        flags = [arg for arg in sys.argv[2:] if arg.startswith('--')]
        server = LeapServer(tag, flags=flags)
        server.run()
    else:
        client = LeapClient(tag)
        client.run()


def _show_usage() -> None:
    """Display usage information."""
    print("Leap - Multi-session AI CLI with message queueing")
    print()
    print("Usage:")
    print("  leap <tag>              Start server (if not running) or client")
    print("  leap <tag> [--flags]    Start server with CLI flags")
    print()
    print("Commands:")
    print("  leap-server <tag>       Start server explicitly")
    print("  leap-client <tag>       Start client explicitly")
    print("  leap-monitor            Open session monitor GUI")
    print()
    print("Examples:")
    print("  Tab 1: leap my-feature          # Starts server")
    print("  Tab 2: leap my-feature          # Starts client")
    print()


if __name__ == '__main__':
    main()
