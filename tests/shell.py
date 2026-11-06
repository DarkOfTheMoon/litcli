"""A shell that actually executes here, for the tests that need one.

On Windows ``bash`` on PATH is the WSL stub (``C:\\Windows\\System32\\bash.exe``), which exits 1
with a UTF-16 "no installed distributions" notice when no distro is set up. Git Bash ships with
the CI runner and with Git for Windows and does run the scripts.
"""
import os
import shutil

_CANDIDATES = (r"C:\Program Files\Git\bin\bash.exe",
               r"C:\Program Files\Git\usr\bin\bash.exe")


def _find() -> str | None:
    if os.name == "nt":
        for candidate in _CANDIDATES:
            if os.path.exists(candidate):
                return candidate
        return None
    return shutil.which("bash")


#: Path to a usable bash, or ``None`` on a host without one.
BASH = _find()
