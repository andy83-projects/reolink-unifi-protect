"""Remember the firmware version adopted from Protect's pushes.

process_upgrade() only changes args.fw_version in memory, so every restart
fell back to --fw-version and Protect pushed (and the camera page showed a
"downgrade") all over again. With --fw-state-file the adopted version is
written to a small JSON file and read back at startup.

The file only ever raises the version: if --fw-version is bumped past it
(e.g. in compose), the flag wins and the file is overwritten on the next push.
"""

import json
import logging
import os
import re
import tempfile
from typing import Optional

# Short form "5.5.45" (--sysid mode) or the numbers inside a legacy long
# form "UVC.S2L.v4.23.8.67.0eba6e3.200526.1046".
_VERSION = re.compile(r"(?:^|v)(\d+)\.(\d+)\.(\d+)")


def version_key(version: str) -> Optional[tuple[int, int, int]]:
    m = _VERSION.search(version or "")
    return tuple(int(g) for g in m.groups()) if m else None  # type: ignore


def is_short_form(version: str) -> bool:
    return re.fullmatch(r"\d+\.\d+\.\d+", version or "") is not None


def load(path: str, current: str, short_form: bool, logger: logging.Logger) -> str:
    """Return the version to advertise: the stored one if it is newer."""
    try:
        with open(path) as f:
            stored = json.load(f).get("fw_version")
    except FileNotFoundError:
        return current
    except Exception as e:
        logger.warning(f"Ignoring unreadable firmware state file {path}: {e}")
        return current

    if not isinstance(stored, str) or is_short_form(stored) != short_form:
        logger.warning(
            f"Ignoring firmware state {stored!r} from {path}: wrong form for"
            f" {'--sysid' if short_form else 'legacy'} mode"
        )
        return current
    new, cur = version_key(stored), version_key(current)
    if new is None or (cur is not None and new <= cur):
        return current
    logger.info(f"Advertising firmware {stored} (from {path}; flag was {current})")
    return stored


def save(path: str, version: str, logger: logging.Logger) -> None:
    """Atomically write the adopted version; never raises."""
    try:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".fw-state-")
        with os.fdopen(fd, "w") as f:
            json.dump({"fw_version": version}, f)
            f.write("\n")
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"Could not save firmware state to {path}: {e}")
