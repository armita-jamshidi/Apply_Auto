"""The separate browser profile that hand-off and the form agent open, and whether it is busy."""

import os
import socket
from pathlib import Path

from agent.settings import PROJECT_ROOT

BROWSER_PROFILE_DIR = PROJECT_ROOT / ".playwright" / "profile"
PROFILE_IN_USE_MESSAGE = (
    "The agent's browser window is still open from an earlier Fill with agent or hand-off. "
    "Finish or close that window, then try again."
)


def browser_profile_in_use(profile: Path | None = None) -> bool:
    """Whether a browser is still running on this profile.

    Chrome lets one browser use a profile at a time. A second launch on the same profile
    passes its blank start page to the running browser and exits, which leaves an empty
    about:blank tab and no agent. So check before launching.
    """
    profile = (profile or BROWSER_PROFILE_DIR).expanduser()
    # Windows: the running browser holds "lockfile" open, so it cannot be opened for writing.
    lock = profile / "lockfile"
    if lock.is_file():
        try:
            lock.open("a").close()
        except PermissionError:
            return True
    # macOS and Linux: "SingletonLock" links to "<host>-<pid>" of the running browser.
    singleton = profile / "SingletonLock"
    if os.name != "nt" and singleton.is_symlink():
        host, _, pid = os.readlink(singleton).rpartition("-")
        if host == socket.gethostname() and pid.isdigit():
            return _process_running(int(pid))
    return False


def _process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # signal 0 only checks the process exists (POSIX)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
