"""Protect image settings -> Reolink SetImage/SetIsp, with a fake camera API."""

import argparse
import asyncio
import logging

from unifi.cams.rtsp import RTSPCam
from unifi.reolink_isp import ReolinkIsp, plan_changes, scale

LOG = logging.getLogger("test")


def test_scale_maps_protect_midpoint_to_reolink_midpoint():
    assert scale(0) == 0
    assert scale(50) == 128
    assert scale(100) == 255


def test_image_sliders_scale():
    image, isp = plan_changes({"brightness": 100, "sharpness": 0}, {})
    assert image == {"bright": 255, "sharpen": 0}
    assert isp == {}


def test_wdr_uses_hdr_on_cameras_that_have_it():
    assert plan_changes({"wdr": 2}, {"hdr": 0})[1] == {"hdr": 1}
    assert plan_changes({"wdr": 0}, {"hdr": 1})[1] == {"hdr": 0}


def test_wdr_uses_drc_otherwise():
    assert plan_changes({"wdr": 0}, {"backLight": "Off"})[1] == {"backLight": "Off"}
    assert plan_changes({"wdr": 3}, {"backLight": "Off"})[1] == {
        "backLight": "DynamicRangeControl",
        "drc": 255,
    }


def test_denoise_mirror_flip():
    _, isp = plan_changes({"denoise": 0, "mirror": 1, "flip": 0}, {})
    assert isp == {"nr3d": 0, "mirroring": 1, "rotation": 0}


class FakeCam(ReolinkIsp):
    def __init__(self, isp, fail=False):
        super().__init__("cam", "u", "p", LOG)
        self.isp = isp
        self.fail = fail
        self.calls = []

    async def _post(self, query, body):
        self.calls.append(body)
        out = []
        for c in body:
            if c["cmd"] == "Login":
                out.append({"code": 0, "value": {"Token": {"name": "t"}}})
            elif c["cmd"] == "GetIsp":
                out.append({"code": 0, "value": {"Isp": dict(self.isp)}})
            else:
                out.append({"code": 1 if self.fail else 0})
        return out


def sent(cam, cmd):
    return [c for call in cam.calls for c in call if c["cmd"] == cmd]


def test_apply_writes_image_and_merged_isp():
    cam = FakeCam({"channel": 0, "hdr": 0, "blc": 128})
    asyncio.run(cam.apply({"brightness": 100, "wdr": 1, "aeMode": "auto"}))
    assert sent(cam, "SetImage")[0]["param"]["Image"] == {"channel": 0, "bright": 255}
    assert sent(cam, "SetIsp")[0]["param"]["Isp"] == {
        "channel": 0,
        "hdr": 1,
        "blc": 128,
    }


def test_apply_skips_unchanged_and_irrelevant():
    cam = FakeCam({"channel": 0})
    asyncio.run(cam.apply({"brightness": 50}))
    n = len(cam.calls)
    asyncio.run(cam.apply({"brightness": 50}))
    asyncio.run(cam.apply({"aeMode": "auto"}))
    assert len(cam.calls) == n


def test_failed_write_is_retried_next_time():
    cam = FakeCam({"channel": 0}, fail=True)
    asyncio.run(cam.apply({"contrast": 60}))
    cam.fail = False
    asyncio.run(cam.apply({"contrast": 60}))
    assert len(sent(cam, "SetImage")) == 2


def test_rtspcam_hooks_are_off_without_flag():
    c = RTSPCam.__new__(RTSPCam)
    c.args = argparse.Namespace(reolink_isp=False, reolink_ai_host="cam")
    asyncio.run(c.change_video_settings({"brightness": 10}))
    assert not hasattr(c, "_isp")
