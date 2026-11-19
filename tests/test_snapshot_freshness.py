"""A live ffmpeg process does not guarantee a current snapshot."""

import argparse
import asyncio
import logging
import os
import time
from pathlib import Path

import pytest

from unifi.cams.rtsp import RTSPCam


class Capture:
    def __init__(self, rc=None):
        self.rc = rc
        self.killed = False

    def poll(self):
        return self.rc

    def kill(self):
        self.killed = True
        self.rc = -9

    def wait(self, timeout):
        return self.rc


def make_cam(tmp_path):
    cam = RTSPCam.__new__(RTSPCam)
    cam.logger = logging.getLogger("test")
    cam.snapshot_dir = str(tmp_path)
    cam.snapshot_stream = Capture()
    cam.args = argparse.Namespace(source=["rtsp://cam/sub"], rtsp_transport="tcp")
    return cam


@pytest.mark.parametrize("dead", [False, True])
async def test_stale_frame_is_replaced_before_returning(tmp_path, monkeypatch, dead):
    cam = make_cam(tmp_path)
    old_capture = cam.snapshot_stream
    old_capture.rc = 1 if dead else None
    frame = tmp_path / "screen.jpg"
    frame.write_bytes(b"last night's frame")
    os.utime(frame, (time.time() - 3600,) * 2)
    writers = []

    async def fresh_frame():
        await asyncio.sleep(0.01)
        frame.write_bytes(b"today's subject")

    def spawn(argv, **kwargs):
        assert not frame.exists()
        assert not kwargs.get("shell")
        assert "-rw_timeout" in argv
        assert "-atomic_writing" in argv
        writers.append(asyncio.create_task(fresh_frame()))
        return Capture()

    monkeypatch.setattr("unifi.cams.rtsp.subprocess.Popen", spawn)
    result = await cam.get_snapshot()
    await asyncio.gather(*writers)
    assert result.read_bytes() == b"today's subject"
    assert old_capture.killed is not dead


async def test_unrecoverable_capture_never_returns_stale_frame(tmp_path, monkeypatch):
    cam = make_cam(tmp_path)
    frame = tmp_path / "screen.jpg"
    frame.write_bytes(b"old frame")
    os.utime(frame, (time.time() - 3600,) * 2)
    monkeypatch.setattr("unifi.cams.rtsp.subprocess.Popen", lambda *a, **k: Capture())

    async def no_wait(delay):
        pass

    monkeypatch.setattr("unifi.cams.rtsp.asyncio.sleep", no_wait)
    with pytest.raises(FileNotFoundError, match="fresh camera snapshot"):
        await cam.get_snapshot()
    assert not frame.exists()


async def test_healthy_capture_is_kept(tmp_path, monkeypatch):
    cam = make_cam(tmp_path)
    frame = Path(cam.snapshot_dir, "screen.jpg")
    frame.write_bytes(b"current frame")

    def unexpected_restart(*args, **kwargs):
        raise AssertionError("healthy capture restarted")

    monkeypatch.setattr("unifi.cams.rtsp.subprocess.Popen", unexpected_restart)
    assert await cam.get_snapshot() == frame
    assert not cam.snapshot_stream.killed
