"""Drive a Reolink camera's pan/tilt/zoom motors from Protect's ``ContinuousMove``.

Protect sends one ``ContinuousMove`` per joystick/zoom-slider tick while held, each with a
``{x, y, z}`` vector on roughly a -1000..1000 "normalized" scale, and a final all-zero message on
release. Observed empirically against a real Reolink E1 Zoom (session 2026-10-12): pan/tilt and
zoom arrive as separate messages (a zoom tick has no x/y keys at all), ``x>0`` is right, ``y>0`` is
up. Reolink's own ``PtzCtrl`` (see the official HTTP API guide) takes a discrete, speed-scaled op
instead of a vector, and a single ``Stop`` halts whatever is moving. The guide lists diagonal pan/
tilt ops (LeftUp, RightDown, ...) and says ``ZoomDec``/``ZoomInc`` are zoom in/out, but on this
camera the diagonal ops return success (code 0) and never move it, and the zoom pair is the
opposite of the doc: confirmed by directly commanding the camera and watching it, not assumed.
So only the four cardinal pan/tilt ops are ever sent (picking whichever axis is dominant when a
drag is imprecisely diagonal), and ``z>0`` (what the UI treats as zoom in) maps to ``ZoomInc``.
"""

import asyncio
import logging
from typing import Any, Optional

import aiohttp

# Below this (on the -1000..1000 scale) a tick counts as released, matching the all-zero
# message Protect sends on release rather than requiring an exact 0.
DEADZONE = 20
MIN_SPEED = 1
MAX_SPEED = 64


def plan_move(x: int, y: int, z: int) -> Optional[tuple[str, int]]:
    """Return (Reolink op, speed) for a ContinuousMove vector, or None to send Stop.

    Pan/tilt takes priority if both are present (Protect's own messages never mix them with
    zoom), and speed is the strongest axis scaled onto Reolink's 1-64 range.
    """
    if abs(x) >= DEADZONE or abs(y) >= DEADZONE:
        # This camera's firmware accepts diagonal ops but never actually moves for them
        # (confirmed by sending one directly and watching); only a true cardinal op does
        # anything, so pick whichever axis the drag leaned on harder.
        if abs(x) >= abs(y):
            op = "Right" if x > 0 else "Left"
        else:
            op = "Up" if y > 0 else "Down"
        return op, _scale_speed(max(abs(x), abs(y)))
    if abs(z) >= DEADZONE:
        return ("ZoomInc" if z > 0 else "ZoomDec"), _scale_speed(abs(z))
    return None


def _scale_speed(magnitude: int) -> int:
    return max(MIN_SPEED, min(MAX_SPEED, round(abs(magnitude) / 1000 * MAX_SPEED)))


class ReolinkPtz:
    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        logger: logging.Logger,
        channel: int = 0,
    ) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.logger = logger
        self.channel = channel
        self._token: Optional[str] = None
        self._lock = asyncio.Lock()
        self._last_op: Optional[str] = None

    async def _post(self, query: str, body: list) -> list:
        url = f"https://{self.host}/cgi-bin/api.cgi?{query}"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=body, ssl=False, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                return await resp.json(content_type=None)

    async def _login(self) -> None:
        res = await self._post(
            "cmd=Login",
            [
                {
                    "cmd": "Login",
                    "param": {
                        "User": {
                            "Version": "0",
                            "userName": self.user,
                            "password": self.password,
                        }
                    },
                }
            ],
        )
        self._token = res[0]["value"]["Token"]["name"]

    async def _ptz_ctrl(self, param: dict[str, Any]) -> bool:
        for attempt in (0, 1):
            if self._token is None:
                await self._login()
            res = await self._post(
                f"token={self._token}",
                [{"cmd": "PtzCtrl", "param": {"channel": self.channel, **param}}],
            )
            r = res[0]
            # rspCode -6 / -7 mean the token is no longer valid; any other failure
            # (bad op, camera busy, ...) is reported as-is, not blindly retried.
            if (
                attempt == 0
                and r.get("code") != 0
                and r.get("error", {}).get("rspCode") in (-6, -7)
            ):
                self._token = None
                continue
            return r.get("code") == 0

    async def continuous_move(self, x: int, y: int, z: int) -> None:
        """Apply one ContinuousMove tick. Never raises; a failure just logs and stops."""
        move = plan_move(x, y, z)
        op = move[0] if move else "Stop"
        if op == self._last_op:
            return  # same direction/stop already in effect; Reolink has no speed-only update
        async with self._lock:
            try:
                param = {"op": op}
                if move:
                    param["speed"] = move[1]
                ok = await self._ptz_ctrl(param)
                if ok:
                    self._last_op = op
                    self.logger.info(
                        "Reolink PTZ op %s (speed %s)", op, param.get("speed", "-")
                    )
                else:
                    self.logger.warning("Reolink PTZ rejected op %s", op)
            except Exception as exc:  # noqa: BLE001
                self._token = None
                self.logger.warning("Could not send PTZ op %s: %s", op, exc)
