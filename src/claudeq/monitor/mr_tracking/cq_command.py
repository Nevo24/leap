"""Data model and message formatting for /cq commands."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CqCommand:
    """A /cq command detected in a GitLab MR discussion thread."""
    project_path: str
    mr_iid: int
    mr_title: str
    mr_url: str
    discussion_id: str
    thread_notes: list[dict]  # [{author, body, created_at}, ...]
    file_path: str | None = None
    old_line: int | None = None
    new_line: int | None = None
    code_snippet: str | None = None


def format_cq_message(cmd: CqCommand) -> str:
    """Format a CqCommand into a plain-text message for the CQ session."""
    parts = []

    # Header
    header = f"MR !{cmd.mr_iid}: \"{cmd.mr_title}\""
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
    parts.append(f"Please address the review feedback above from MR !{cmd.mr_iid}.")
    parts.append(f"MR URL: {cmd.mr_url}")

    return "\n".join(parts)
