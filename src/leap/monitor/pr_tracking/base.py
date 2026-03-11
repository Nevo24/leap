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
    approved: bool = False
    approved_by: Optional[list[str]] = None


@dataclass
class UserNotification:
    """A notification from an SCM provider (GitLab Todo / GitHub Notification)."""
    id: str
    scm_type: str  # "gitlab" or "github"
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
    def get_pr_status(self, project_path: str, branch: str) -> PRStatus:
        """Get PR status for a project/branch combination.

        Args:
            project_path: The project path (e.g., 'user/repo').
            branch: The source branch name.

        Returns:
            PRStatus with current state and details.
        """

    @abstractmethod
    def scan_leap_commands(self, project_path: str, branch: str) -> list:
        """Scan for /leap commands in PR discussion threads.

        Args:
            project_path: The project path (e.g., 'user/repo').
            branch: The source branch name.

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
    def collect_unresponded_threads(self, project_path: str, branch: str) -> list:
        """Collect all unresponded discussion threads from a PR as CqCommand objects.

        Args:
            project_path: The project path (e.g., 'user/repo').
            branch: The source branch name.

        Returns:
            List of CqCommand instances for each unresponded thread.
        """

    def supports_notifications(self) -> bool:
        """Whether this provider supports user notification tracking."""
        return False

    def get_user_notifications(self) -> list[UserNotification]:
        """Get pending user notifications (GitLab Todos / GitHub Notifications).

        Returns:
            List of UserNotification instances.
        """
        return []
