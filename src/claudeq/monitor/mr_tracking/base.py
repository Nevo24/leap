"""Provider-agnostic types for SCM integration."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class MRState(Enum):
    """State of a merge/pull request."""
    NOT_CONFIGURED = "not_configured"
    NO_MR = "no_mr"
    ALL_RESPONDED = "all_responded"
    UNRESPONDED = "unresponded"


@dataclass
class MRDetails:
    """Basic details of a merge/pull request."""
    source_branch: str
    mr_title: str
    mr_url: str


@dataclass
class MRStatus:
    """Status of a merge/pull request for a session."""
    state: MRState
    unresponded_count: int = 0
    mr_url: Optional[str] = None
    mr_title: Optional[str] = None
    mr_iid: Optional[int] = None
    first_unresponded_note_id: Optional[int] = None
    approved: bool = False
    approved_by: Optional[list[str]] = None


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
    def get_mr_details(self, project_path: str, mr_iid: int) -> Optional[MRDetails]:
        """Get basic MR details by IID.

        Args:
            project_path: The project path (e.g., 'user/repo').
            mr_iid: The MR/PR number.

        Returns:
            MRDetails or None if not found.
        """

    @abstractmethod
    def get_mr_status(self, project_path: str, branch: str) -> MRStatus:
        """Get MR status for a project/branch combination.

        Args:
            project_path: The project path (e.g., 'user/repo').
            branch: The source branch name.

        Returns:
            MRStatus with current state and details.
        """

    @abstractmethod
    def scan_cq_commands(self, project_path: str, branch: str) -> list:
        """Scan for /cq commands in MR discussion threads.

        Args:
            project_path: The project path (e.g., 'user/repo').
            branch: The source branch name.

        Returns:
            List of CqCommand instances found.
        """

    @abstractmethod
    def acknowledge_cq_command(self, project_path: str, mr_iid: int, discussion_id: str) -> bool:
        """Post acknowledgment reply to a /cq thread.

        Returns:
            True on success.
        """

    @abstractmethod
    def report_no_session(self, project_path: str, mr_iid: int, discussion_id: str) -> bool:
        """Post error reply when no matching CQ session is found.

        Returns:
            True on success.
        """

    @abstractmethod
    def collect_unresponded_threads(self, project_path: str, branch: str) -> list:
        """Collect all unresponded discussion threads from an MR as CqCommand objects.

        Args:
            project_path: The project path (e.g., 'user/repo').
            branch: The source branch name.

        Returns:
            List of CqCommand instances for each unresponded thread.
        """
