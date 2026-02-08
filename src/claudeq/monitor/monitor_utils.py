"""Standalone utility functions for ClaudeQ Monitor."""

from pathlib import Path
from typing import Optional

from PyQt5.QtWidgets import QMessageBox

from claudeq.monitor.session_manager import load_session_metadata, session_exists
from claudeq.monitor.navigation import find_terminal_with_title


def find_icon() -> Optional[Path]:
    """Find the app icon, works both from source and .app bundle."""
    # From source: src/claudeq/monitor/monitor_utils.py → project_root/assets/
    candidate = Path(__file__).parent.parent.parent.parent / "assets" / "claudeq-icon.png"
    if candidate.exists():
        return candidate

    # From .app bundle: walk up to Contents/Resources/
    for parent in Path(__file__).parents:
        if parent.name == 'Resources' and parent.parent.name == 'Contents':
            candidate = parent / "claudeq-icon.png"
            if candidate.exists():
                return candidate
            break

    return None


def focus_session(tag: str, session_type: str = 'server') -> None:
    """
    Focus the terminal with the given session.

    Args:
        tag: Session tag name.
        session_type: 'server' or 'client'.
    """
    metadata = load_session_metadata(tag)

    preferred_ide = metadata.get('ide') if metadata else None
    project_path = metadata.get('project_path') if metadata else None
    title_pattern = f"cq-{session_type} {tag}"

    # Check if session exists
    if not session_exists(tag, session_type):
        other_type = 'server' if session_type == 'client' else 'client'
        reply = QMessageBox.question(
            None,
            f'{session_type.capitalize()} Not Found',
            f'{session_type.capitalize()} not found for: {tag}\n\n'
            f'Go to {other_type} instead?',
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            focus_session(tag, other_type)
        return

    # Try to find and focus the terminal
    result = find_terminal_with_title(
        title_pattern,
        preferred_ide,
        project_path,
        title_pattern
    )

    if not result:
        QMessageBox.warning(
            None,
            'Navigation Failed',
            f'Could not navigate to {session_type}: {tag}\n\n'
            'Make sure terminal tab titles are configured correctly.'
        )
