"""Provider-agnostic types for SCM integration."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class PRState(Enum):
    """State of a pull request."""
    NOT_CONFIGURED = "not_configured"
    NO_PR = "no_pr"
    ALL_RESPONDED = "all_responded"
    UNRESPONDED = "unresponded"


@dataclass
class PRDetails:
    """Basic details of a pull request."""
    source_branch: str
    pr_title: str
    pr_url: str
    source_branch_deleted: bool = False


@dataclass
class PRStatus:
    """Status of a pull request for a session."""
    state: PRState
    unresponded_count: int = 0
    pr_url: Optional[str] = None
    pr_title: Optional[str] = None
    pr_iid: Optional[int] = None
    first_unresponded_note_id: Optional[int] = None
    # Pre-built deep-link to the first unresponded comment.  Provider-specific
    # anchor format (``#note_<id>`` for GitLab, ``#discussion_r<id>`` for
    # GitHub).  When None and we want a deep-link, render code falls back to
    # ``pr_url`` so users still land on the PR root.
    first_unresponded_url: Optional[str] = None
    approved: bool = False
    approved_by: Optional[list[str]] = None
    self_approved: bool = False  # True if the current user is among the approvers
    # False when the approvals fetch failed and no prior value was available,
    # i.e. the approval state for this poll is *unknown* rather than a definite
    # "nobody approved".  The notification diff skips approval comparison when
    # either side is unknown, so a failed-then-recovered fetch is not mistaken
    # for a brand-new approval.
    approval_known: bool = True
    draft: bool = False  # True if the PR is a draft / work-in-progress
    has_conflicts: bool = False  # True if the PR cannot be merged (conflicts)
    changes_requested: bool = False  # True if a reviewer requested changes
    checks_failed: bool = False  # True if CI / pipeline checks are failing


@dataclass
class ClosedPRInfo:
    """Summary of a non-open PR surfaced when no open PR matches a branch."""
    pr_iid: int
    pr_title: str
    pr_url: str
    merged: bool


@dataclass
class UserNotification:
    """A notification from an SCM provider (GitLab Todo / GitHub Notification /
    Bitbucket Server reviewer-dashboard entry)."""
    id: str
    scm_type: str  # "gitlab", "github" or "bitbucket"
    reason: str  # "review_requested", "assigned", "mentioned", "other"
    title: str  # Target title (PR/issue title)
    target_url: str  # URL to open in browser
    project_name: Optional[str] = None
    author: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class ConnectionTestResult:
    """Result of a connection test with permission details."""
    success: bool
    username: str  # username on success, error message on failure
    warnings: list[str]  # permission warnings (empty = all permissions present)


class SCMProvider(ABC):
    """Abstract base class for SCM providers (GitLab, GitHub, etc.)."""

    @abstractmethod
    def test_connection(self) -> tuple[bool, str]:
        """Test the connection to the SCM provider.

        Returns:
            Tuple of (success, message). Message is username on success, error on failure.
        """

    @abstractmethod
    def get_username(self) -> Optional[str]:
        """Get the authenticated username."""

    @abstractmethod
    def get_pr_details(self, project_path: str, pr_iid: int) -> Optional[PRDetails]:
        """Get basic PR details by IID.

        Args:
            project_path: The project path (e.g., 'user/repo').
            pr_iid: The PR number.

        Returns:
            PRDetails or None if not found.
        """

    @abstractmethod
    def get_pr_status(self, project_path: str, branch: str,
                      pr_iid: Optional[int] = None) -> PRStatus:
        """Get PR status for a project/branch combination.

        Args:
            project_path: The project path (e.g., 'user/repo').
            branch: The source branch name.
            pr_iid: Optional PR number. When supplied, providers MAY
                bypass branch-based listing and fetch the PR directly —
                required to track GitHub fork PRs (whose source branch
                lives in a different repo from the base).

        Returns:
            PRStatus with current state and details.
        """

    @abstractmethod
    def scan_leap_commands(self, project_path: str, branch: str,
                           pr_iid: Optional[int] = None) -> list:
        """Scan for /leap commands in PR discussion threads.

        Args:
            project_path: The project path (e.g., 'user/repo').
            branch: The source branch name.
            pr_iid: Optional PR number — see ``get_pr_status``.

        Returns:
            List of CqCommand instances found.
        """

    @abstractmethod
    def acknowledge_leap_command(self, project_path: str, pr_iid: int, discussion_id: str) -> bool:
        """Post acknowledgment reply to a /leap thread.

        Returns:
            True on success.
        """

    @abstractmethod
    def report_no_session(self, project_path: str, pr_iid: int, discussion_id: str) -> bool:
        """Post error reply when no matching Leap session is found.

        Returns:
            True on success.
        """

    @abstractmethod
    def collect_unresponded_threads(self, project_path: str, branch: str,
                                    pr_iid: Optional[int] = None) -> list:
        """Collect all unresponded discussion threads from a PR as CqCommand objects.

        Args:
            project_path: The project path (e.g., 'user/repo').
            branch: The source branch name.
            pr_iid: Optional PR number — see ``get_pr_status``.

        Returns:
            List of CqCommand instances for each unresponded thread.
        """

    def find_latest_closed_pr(self, project_path: str,
                              branch: str) -> Optional[ClosedPRInfo]:
        """Return the most recently updated non-open PR for *branch*.

        Used when an open-PR lookup returned NO_PR but a closed or merged
        PR for the same branch is worth surfacing to the user.

        Default returns None — providers opt in by overriding.
        """
        del project_path, branch
        return None

    def supports_notifications(self) -> bool:
        """Whether this provider supports user notification tracking."""
        return False

    def get_user_notifications(self) -> list[UserNotification]:
        """Get pending user notifications (GitLab Todos / GitHub Notifications).

        Returns:
            List of UserNotification instances.
        """
        return []

    def build_first_unresponded_url(self, pr_url: str, comment_id: int,
                                    origin: str = 'r') -> str:
        """Build a deep-link URL that scrolls to *comment_id* on *pr_url*.

        ``origin`` is provider-specific: GitHub uses ``'r'`` for review
        comments (anchor ``#discussion_r<id>``) and ``'i'`` for issue
        comments (anchor ``#issuecomment-<id>``).  GitLab ignores it.

        Subclasses override with the platform-specific anchor format.
        Default returns the bare *pr_url* — never raises.
        """
        del comment_id, origin
        return pr_url
