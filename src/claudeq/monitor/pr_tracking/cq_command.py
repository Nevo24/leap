"""Data model and message formatting for /cq commands from SCM PR discussion threads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class CqCommand:
    """A /cq command detected in an SCM PR discussion thread."""
    project_path: str
    pr_iid: int
    pr_title: str
    pr_url: str
    discussion_id: str
    thread_notes: list[dict[str, str]]  # [{author, body, created_at}, ...]
    file_path: Optional[str] = None
    old_line: Optional[int] = None
    new_line: Optional[int] = None
    code_snippet: Optional[str] = None


def format_cq_message(cmd: CqCommand) -> str:
    """Format a CqCommand into a plain-text message for the CQ session."""
    parts = []

    # Header
    header = f"PR !{cmd.pr_iid}: \"{cmd.pr_title}\""
    if cmd.thread_notes:
        first_author = cmd.thread_notes[0].get('author', 'unknown')
        header += f" — Thread from @{first_author}"
    if cmd.file_path:
        line = cmd.new_line or cmd.old_line
        header += f" on {cmd.file_path}"
        if line:
            header += f":{line}"
    parts.append(header)
    parts.append("")

    # Code context
    if cmd.file_path and cmd.code_snippet:
        line = cmd.new_line or cmd.old_line
        line_info = f", line {line}" if line else ""
        parts.append(f"Code context ({cmd.file_path}{line_info}):")
        parts.append("```")
        parts.append(cmd.code_snippet)
        parts.append("```")
        parts.append("")

    # Thread conversation
    if cmd.thread_notes:
        parts.append("Thread:")
        for note in cmd.thread_notes:
            author = note.get('author', 'unknown')
            body = note.get('body', '').strip()
            if body.strip() == '/cq':
                continue
            parts.append(f"- @{author}: \"{body}\"")
        parts.append("")

    # Closing instruction
    parts.append(f"Please address the review feedback above from PR !{cmd.pr_iid}.")
    parts.append(f"PR URL: {cmd.pr_url}")

    return "\n".join(parts)
