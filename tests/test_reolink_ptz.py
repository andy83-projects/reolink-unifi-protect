"""Protect's PTZ joystick/zoom -> Reolink PtzCtrl, with a fake camera API only.

Sign conventions (x>0=right, y>0=up, z>0=zoom in), the "zoom and pan/tilt never arrive
together" shape, that diagonal pan/tilt ops are accepted but never move this camera, and that
its zoom pair is the opposite of the official doc's naming, were all read from a real Reolink
E1 Zoom during manual testing (session 2026-10-12), not guessed; see reolink_ptz.py's module
docstring.
"""

import argparse
import asyncio
import logging

from unifi.cams.rtsp import RTSPCam
from unifi.reolink_ptz import ReolinkPtz, plan_move

LOG = logging.getLogger("test")


def test_deadzone_is_a_stop():
    assert plan_move(0, 0, 0) is None
    assert plan_move(5, -5, 10) is None


def test_cardinal_directions():
    assert plan_move(1000, 0, 0)[0] == "Right"
    assert plan_move(-1000, 0, 0)[0] == "Left"
    assert plan_move(0, 1000, 0)[0] == "Up"
    assert plan_move(0, -1000, 0)[0] == "Down"


def test_diagonal_drags_pick_the_dominant_axis_instead_of_a_diagonal_op():
    # Diagonal ops (LeftUp, RightDown, ...) are in the official doc and this camera's
    # GetAbility, and it answers code 0 for them, but never actually moves: confirmed by
    # sending one directly and watching the camera. So a diagonal-leaning drag has to
    # resolve to whichever cardinal axis is strongest, never a diagonal op.
    assert plan_move(1000, 999, 0)[0] == "Right"
    assert plan_move(999, 1000, 0)[0] == "Up"
    assert plan_move(-1000, -999, 0)[0] == "Left"
    assert plan_move(-999, -1000, 0)[0] == "Down"


def test_small_jitter_on_one_axis_stays_cardinal():
    assert plan_move(1000, 5, 0)[0] == "Right"
    assert plan_move(5, -1000, 0)[0] == "Down"


def test_pan_tilt_takes_priority_over_zoom():
    # Protect's own messages never mix them, but the mapping shouldn't assume that.
    op, _ = plan_move(1000, 0, 1000)
    assert op == "Right"


def test_zoom_direction_is_the_opposite_of_the_official_docs_naming():
    # The official doc says ZoomDec is zoom in and ZoomInc is zoom out, but on this camera
    # that's backwards: confirmed by commanding each op directly and watching it zoom the
    # wrong way. z>0 (what the UI treats as zoom in) has to map to ZoomInc here.
    assert plan_move(0, 0, 1000)[0] == "ZoomInc"
    assert plan_move(0, 0, -1000)[0] == "ZoomDec"


def test_speed_scales_from_the_strongest_axis():
    assert plan_move(1000, 0, 0)[1] == 64
    assert plan_move(500, 0, 0)[1] == 32
    assert plan_move(20, 0, 0)[1] == 1  # at the deadzone edge, never 0


class FakeCam(ReolinkPtz):
    def __init__(self, fail=False):
        super().__init__("cam", "u", "p", LOG)
        self.fail = fail
        self.calls = []

    async def _post(self, query, body):
        self.calls.append(body)
        out = []
        for c in body:
            if c["cmd"] == "Login":
                out.append({"code": 0, "value": {"Token": {"name": "t"}}})
            else:
                out.append({"code": 1 if self.fail else 0})
        return out


def sent_ops(cam):
    return [
        c["param"]["op"] for call in cam.calls for c in call if c["cmd"] == "PtzCtrl"
    ]


def test_continuous_move_sends_the_op():
    cam = FakeCam()
    asyncio.run(cam.continuous_move(1000, 0, 0))
    assert sent_ops(cam) == ["Right"]


def test_same_direction_is_not_resent():
    cam = FakeCam()
    asyncio.run(cam.continuous_move(1000, 0, 0))
    asyncio.run(
        cam.continuous_move(900, 5, 0)
    )  # still "Right": y=5 is inside the deadzone
    assert sent_ops(cam) == ["Right"]


def test_release_sends_stop_once():
    cam = FakeCam()
    asyncio.run(cam.continuous_move(1000, 0, 0))
    asyncio.run(cam.continuous_move(0, 0, 0))
    asyncio.run(cam.continuous_move(0, 0, 0))
    assert sent_ops(cam) == ["Right", "Stop"]


def test_failed_op_is_retried_next_tick():
    cam = FakeCam(fail=True)
    asyncio.run(cam.continuous_move(1000, 0, 0))
    cam.fail = False
    asyncio.run(cam.continuous_move(1000, 0, 0))
    assert sent_ops(cam) == ["Right", "Right"]


def make_ptz_cam(ptz_flag=True, host="cam"):
    c = RTSPCam.__new__(RTSPCam)
    c.args = argparse.Namespace(
        ptz=ptz_flag, reolink_ai_host=host, reolink_ai_user="u", reolink_ai_password="p"
    )
    c.logger = LOG
    return c


def test_rtspcam_hook_off_without_ptz_flag():
    c = make_ptz_cam(ptz_flag=False)
    asyncio.run(c.continuous_move(1000, 0, 0))
    assert not hasattr(c, "_ptz_task")


def test_rtspcam_hook_off_without_camera_host():
    c = make_ptz_cam(host=None)
    asyncio.run(c.continuous_move(1000, 0, 0))
    assert not hasattr(c, "_ptz_task")


def test_rtspcam_hook_never_cancels_an_in_flight_request_and_drains_the_next_tick():
    # The first cut of this cancelled the in-flight request on every new tick; since
    # Protect sends ticks faster than an HTTP round trip, no request ever survived long
    # enough to reach the camera. The worker must let each send finish, then
    # automatically pick up whatever tick is now latest, with no further message needed.
    c = make_ptz_cam()
    sent = []
    started = asyncio.Event()
    finish = asyncio.Event()

    class SlowPtz:
        async def continuous_move(self_inner, x, y, z):
            started.set()
            await finish.wait()
            sent.append((x, y, z))

    c._ptz = SlowPtz()

    async def scenario():
        await c.continuous_move(1000, 0, 0)
        await started.wait()  # the worker has genuinely picked this up and is blocked
        first = c._ptz_task
        await c.continuous_move(0, 1000, 0)  # must not cancel the in-flight send
        finish.set()
        await first
        return first

    first = asyncio.run(scenario())
    assert not first.cancelled()
    assert sent == [(1000, 0, 0), (0, 1000, 0)]


def test_rtspcam_hook_coalesces_ticks_that_arrive_while_sending():
    # The send already in flight (100,0,0) isn't cancelled and still completes, but a
    # middle tick (200,0,0) that lands before the worker reads the next one is coalesced
    # away by (300,0,0) rather than being queued and sent too.
    c = make_ptz_cam()
    sent = []
    started = asyncio.Event()
    finish = asyncio.Event()

    class SlowPtz:
        async def continuous_move(self_inner, x, y, z):
            started.set()
            await finish.wait()
            sent.append((x, y, z))

    c._ptz = SlowPtz()

    async def scenario():
        await c.continuous_move(100, 0, 0)
        await started.wait()  # the worker is now sending (100,0,0)
        await c.continuous_move(200, 0, 0)  # superseded before the worker reads again
        await c.continuous_move(300, 0, 0)
        finish.set()
        await c._ptz_task

    asyncio.run(scenario())
    assert sent == [(100, 0, 0), (300, 0, 0)]


def test_protect_continuous_move_message_reaches_the_hook_through_process():
    # Real regression: ContinuousMove always has responseExpected=False, and process()'s
    # early-return guard (for messages needing no reply) didn't list it as one that still
    # needs ACTING on, so every joystick tick was silently dropped before reaching any
    # handler. This exercises the real dispatcher, not just the hook directly.
    import json

    c = make_ptz_cam()
    calls = []

    async def hook(x, y, z):
        calls.append((x, y, z))

    c.continuous_move = hook
    msg = json.dumps(
        {
            "functionName": "ContinuousMove",
            "messageId": 1,
            "responseExpected": False,
            "payload": {"x": 500, "y": -250, "z": 0},
        }
    )
    asyncio.run(c.process(msg.encode()))
    assert calls == [(500, -250, 0)]
