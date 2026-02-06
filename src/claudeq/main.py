"""
ClaudeQ main entry point.

Thin dispatcher that routes to the appropriate component based on arguments.
"""

import sys


def main() -> None:
    """
    Main entry point for the 'cq' command.

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

    # Check if server is running
    from claudeq.utils.constants import SOCKET_DIR
    socket_path = SOCKET_DIR / f"{tag}.sock"

    if not socket_path.exists():
        # Start server
        from claudeq.server import ClaudeQServer
        flags = [arg for arg in sys.argv[2:] if arg.startswith('--')]
        server = ClaudeQServer(tag, flags=flags)
        server.run()
    else:
        # Start client
        from claudeq.client import ClaudeQClient
        client = ClaudeQClient(tag)
        client.run()


def _show_usage() -> None:
    """Display usage information."""
    print("ClaudeQ - Multi-session Claude Code with message queueing")
    print()
    print("Usage:")
    print("  cq <tag>              Start server (if not running) or client")
    print("  cq <tag> [--flags]    Start server with Claude CLI flags")
    print()
    print("Commands:")
    print("  cq-server <tag>       Start server explicitly")
    print("  cq-client <tag>       Start client explicitly")
    print("  cq-monitor            Open session monitor GUI")
    print()
    print("Examples:")
    print("  Tab 1: cq my-feature          # Starts server")
    print("  Tab 2: cq my-feature          # Starts client")
    print()


if __name__ == '__main__':
    main()
