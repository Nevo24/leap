"""Persists and replays a saved sudo password for ``pmset`` calls.

The user explicitly opts into saving their account password to disk by
turning on the lid-close override checkbox.  We minimise the risk by:

* Storing under ``.storage/`` with mode ``0600`` so only the user can
  read it.
* Base64-encoding the bytes — *not* encryption — so the file isn't
  trivially grep-able and can't be accidentally read out loud during a
  screen-share.  (See the note below on threat model.)
* Validating the password against ``sudo -S -v`` before saving so a
  typo doesn't get persisted and silently break the feature later.

Honest threat model: anything you can decrypt on this machine, an
attacker with code execution as the same user can also decrypt — they
can read the keychain, mimic the binary, attach to the process, etc.
This file is "secure" only against the threat of a leaked
``.storage/`` archive being grep-ed for plaintext passwords.  It is
NOT a real secret store.  We accept the tradeoff because the user
asked for it explicitly to power a power-management convenience
feature, not a security boundary.
"""

import base64
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from leap.utils.constants import STORAGE_DIR

logger = logging.getLogger(__name__)

# File on disk holding the base64-encoded sudo password.  Path is
# computed lazily inside the methods so test code that monkey-patches
# ``STORAGE_DIR`` picks up the override.
_PASSWORD_FILENAME = 'sudo_pass.b64'


def _password_path() -> Path:
    return STORAGE_DIR / _PASSWORD_FILENAME


class SudoManager:
    """Static helpers — no instance state to coordinate."""

    @staticmethod
    def has_saved() -> bool:
        return _password_path().exists()

    @staticmethod
    def load() -> Optional[str]:
        """Return the saved password, or ``None`` if missing/unreadable.

        Catches every plausible decode failure (file missing, bad
        base64, invalid UTF-8 from a partially-overwritten file) so a
        corrupt password file degrades to "no saved password" rather
        than crashing the caller.
        """
        try:
            with open(_password_path(), 'rb') as f:
                return base64.b64decode(f.read()).decode('utf-8')
        except (OSError, ValueError, UnicodeDecodeError):
            return None

    @staticmethod
    def save(password: str) -> None:
        """Write the password atomically with mode ``0600``.

        Uses ``os.open`` with an explicit mode so the file is *born*
        with 0600 — no umask-derived window in which a 0644 file is
        visible to other users on a multi-user box.  Then ``os.replace``
        atomically moves it onto the final path so the target is
        either fully written or untouched, never partial.
        """
        encoded = base64.b64encode(password.encode('utf-8'))
        path = _password_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        # 0o600 only ever has permission bits cleared by umask, so it
        # is safe regardless of the user's umask setting.
        flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
        fd = os.open(str(tmp), flags, 0o600)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(encoded)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            try:
                os.unlink(str(tmp))
            except OSError:
                pass
            raise
        os.replace(str(tmp), str(path))

    @staticmethod
    def clear() -> None:
        """Delete the saved password file (if any)."""
        try:
            _password_path().unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def verify(password: str) -> bool:
        """Return True iff ``password`` authenticates against ``sudo``.

        Invalidates any cached sudo credential first so a stale earlier
        successful auth in this terminal session cannot mask a wrong
        password from us.
        """
        try:
            subprocess.run(
                ['/usr/bin/sudo', '-k'],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
            r = subprocess.run(
                ['/usr/bin/sudo', '-S', '-v'],
                input=(password + '\n').encode('utf-8'),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=5,
                check=False,
            )
            return r.returncode == 0
        except (subprocess.SubprocessError, OSError):
            logger.exception("sudo verify failed")
            return False

    @staticmethod
    def run(args: list[str], password: str) -> Tuple[int, str]:
        """Execute ``sudo -S <args>`` with the given password.

        Returns ``(returncode, stderr_text)``.  The caller distinguishes
        an auth failure (``returncode != 0`` *and* the stderr contains
        ``Sorry, try again`` or similar) from other errors.
        """
        try:
            r = subprocess.run(
                ['/usr/bin/sudo', '-S', *args],
                input=(password + '\n').encode('utf-8'),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=5,
                check=False,
            )
            return r.returncode, r.stderr.decode('utf-8', errors='replace')
        except (subprocess.SubprocessError, OSError) as e:
            return -1, str(e)

    @staticmethod
    def is_auth_failure(returncode: int, stderr: str) -> bool:
        """Return True if the failure looks like a wrong-password.

        ``sudo -S`` exits with ``1`` and prints messages like
        ``Sorry, try again.`` or ``X incorrect password attempts`` when
        the password is wrong, vs. ``user is not in the sudoers file``
        for a permissions issue we can't fix by re-prompting.
        """
        if returncode == 0:
            return False
        haystack = stderr.lower()
        return (
            'try again' in haystack
            or 'incorrect password' in haystack
            or 'no password was provided' in haystack
        )

    @staticmethod
    def password_path() -> Path:
        """Public accessor for the on-disk path (used in dialog copy)."""
        return _password_path()
