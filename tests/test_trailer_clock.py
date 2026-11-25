"""clock_sync --trailer-clock media: trailer timestamps from FLV media time.

The default (wall) trailer is the time each tag was read from ffmpeg's pipe;
bursty delivery makes it near-duplicate pairs plus 100-300 ms gaps, which
Protect's ms serves to RTSP clients as RTP timestamps. Media mode
keeps the camera's frame spacing and slews toward wall clock.
"""

import argparse
import io
import logging
import shlex
import struct
import sys
import time
import types

import pytest

from unifi import clock_sync
from unifi.cams.rtsp import RTSPCam
from unifi.clock_sync import MediaClock

FLV_HEADER = b"FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00"
VIDEO, AUDIO, SCRIPT = 9, 8, 18


def tag(ptype, ts_ms, payload=b"\x17\x01\x00\x00\x00\x00\x00\x00\x01\x65"):
    size = len(payload)
    ts_ms &= 0xFFFFFFFF
    hdr = bytes([ptype, (size >> 16) & 0xFF, (size >> 8) & 0xFF, size & 0xFF])
    hdr += bytes([(ts_ms >> 16) & 0xFF, (ts_ms >> 8) & 0xFF, ts_ms & 0xFF, ts_ms >> 24])
    return hdr + b"\x00\x00\x00" + payload + (11 + size).to_bytes(4, "big")


def run(tags, arrivals, mode, monkeypatch, audio_clock=None):
    """Run clock_sync.main over (ptype, ts_ms) tags read at wall `arrivals`.

    Returns [(ptype, flv_ts_ms, clock, trailer_ticks)] for every output tag.
    """
    data = FLV_HEADER + b"".join(tag(p, t) for p, t in tags)
    out = io.BytesIO()
    err = io.BytesIO()
    times = iter([1000.0, 1000.0] + [1000.0 + a for a in arrivals])
    monkeypatch.setattr(clock_sync.time, "time", lambda: next(times))
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(data)))
    monkeypatch.setattr(sys, "stdout", types.SimpleNamespace(buffer=out))
    monkeypatch.setattr(sys, "stderr", types.SimpleNamespace(buffer=err))
    clock_sync.main(types.SimpleNamespace(trailer_clock=mode, audio_clock=audio_clock))
    return parse(out.getvalue()), err.getvalue().decode()


def parse(b):
    assert b[:3] == b"FLV"
    i, res = 13, []
    while i < len(b):
        ptype = b[i]
        size = int.from_bytes(b[i + 1 : i + 4], "big")
        ts = int.from_bytes(b[i + 4 : i + 7], "big") | (b[i + 7] << 24)
        i += 11 + size + 4
        tr = b[i : i + 16]
        assert tr[0] == 0 and len(tr) == 16
        clock = int.from_bytes(tr[1:4], "big")
        res.append((ptype, ts, clock, struct.unpack(">I", tr[12:16])[0]))
        i += 16
    assert i == len(b)
    return res


def video_secs(res):
    return [ticks / 90000 for p, _, c, ticks in res if p == VIDEO]


def gaps_ms(xs):
    return [(b - a) * 1000 for a, b in zip(xs, xs[1:])]


def bursty(n, frame=0.04, group=6):
    """Media every `frame` s; arrivals in groups of `group` at the group end."""
    return [((k // group) + 1) * group * frame for k in range(n)]


# --- wall mode is unchanged --------------------------------------------------


def test_wall_mode_stamps_read_time(monkeypatch):
    tags = [(VIDEO, 40 * k) for k in range(20)]
    arrivals = bursty(20)
    res, _ = run(tags, arrivals, "wall", monkeypatch)
    assert [r[3] for r in res] == [int(a * 90000) & 0xFFFFFFFF for a in arrivals]
    # Arg absent (older callers) behaves as wall.
    res2, _ = run(tags, arrivals, None, monkeypatch)
    assert res2 == res


# --- media mode --------------------------------------------------------------


def test_media_mode_smooths_bursty_arrival(monkeypatch):
    n = 750  # 30 s at 25 fps
    res, log = run([(VIDEO, 40 * k) for k in range(n)], bursty(n), "media", monkeypatch)
    g = gaps_ms(video_secs(res))
    assert all(38 <= x <= 42 for x in g), (min(g), max(g))
    assert "resync" not in log
    # Wall mode on the same input: the defect this fixes.
    gw = gaps_ms(
        video_secs(
            run([(VIDEO, 40 * k) for k in range(n)], bursty(n), "wall", monkeypatch)[0]
        )
    )
    assert sum(x == 0 for x in gw) > 500 and max(gw) > 200


def test_media_rate_mismatch_tracks_wall_without_resync(monkeypatch):
    n = 3000  # 120 s; camera clock 1% slow vs wall
    arrivals = [k * 0.0404 for k in range(n)]
    res, log = run([(VIDEO, 40 * k) for k in range(n)], arrivals, "media", monkeypatch)
    v = video_secs(res)
    assert "resync" not in log
    assert abs(arrivals[-1] - v[-1]) < 0.1
    assert all(38 <= x <= 42 for x in gaps_ms(v))


def test_media_stall_resyncs_forward_once(monkeypatch):
    n = 200
    arrivals = [k * 0.04 + (1.0 if k >= 100 else 0.0) for k in range(n)]
    res, log = run([(VIDEO, 40 * k) for k in range(n)], arrivals, "media", monkeypatch)
    assert log.count("resync") == 1
    v = video_secs(res)
    g = gaps_ms(v)
    assert min(g) >= 0  # monotonic
    assert abs(v[-1] - arrivals[-1]) < 0.01


def test_media_startup_burst_drains_without_resync(monkeypatch):
    # 2.4 s of buffered frames delivered at once, then real time.
    n = 600
    arrivals = [0.0 if k < 60 else (k - 60) * 0.04 + 0.04 for k in range(n)]
    res, log = run([(VIDEO, 40 * k) for k in range(n)], arrivals, "media", monkeypatch)
    assert "resync" not in log
    v = video_secs(res)
    g = gaps_ms(v)
    assert min(g) >= 29.9 and max(g) <= 42
    assert abs(v[-1] - arrivals[-1]) < 0.3


def test_media_timestamp_wrap(monkeypatch):
    base = 2**32 - 400
    res, log = run(
        [(VIDEO, base + 40 * k) for k in range(30)],
        [k * 0.04 for k in range(30)],
        "media",
        monkeypatch,
    )
    assert all(abs(x - 40) < 0.5 for x in gaps_ms(video_secs(res)))
    assert log == ""


def test_media_anchors_on_first_media_tag_not_metadata(monkeypatch):
    tags = [(SCRIPT, 0)] + [(VIDEO, 40 * k) for k in range(50)]
    arrivals = [0.0] + [1.5 + k * 0.04 for k in range(50)]
    res, log = run(tags, arrivals, "media", monkeypatch)
    assert log == ""
    v = video_secs(res)
    assert abs(v[0] - 1.5) < 1e-4
    assert all(abs(x - 40) < 0.5 for x in gaps_ms(v))


def test_media_interleaved_audio_video_monotonic(monkeypatch):
    # FLV interleave: audio (64 ms AAC) runs slightly ahead of video.
    tags, arrivals = [], []
    for k in range(400):
        tags.append((VIDEO, 40 * k))
        if k % 8 == 0:
            for j in range(5):
                tags.append((AUDIO, 40 * k + 20 + 64 * j))
    # Delivered in 240 ms bursts by media time (non-decreasing read order).
    arrivals, last = [], 0.0
    for _, ts in tags:
        last = max(last, (ts // 240 + 1) * 0.24)
        arrivals.append(last)
    res, log = run(tags, arrivals, "media", monkeypatch)
    assert "resync" not in log
    v = video_secs(res)
    assert all(38 <= x <= 42 for x in gaps_ms(v))
    for kind in (AUDIO,):
        xs = [t for p, _, c, t in res if p == kind]
        assert all(b >= a for a, b in zip(xs, xs[1:]))


def test_injected_clocksync_trailers_monotonic(monkeypatch):
    # 12 s of video so onClockSync/onMpma get injected (every 5 s).
    n = 300
    tags = [(VIDEO, 40 * k) for k in range(n)]
    res, _ = run(tags, bursty(n), "media", monkeypatch)
    other = [t / 11025 for p, _, c, t in res if p == SCRIPT]
    assert len(other) >= 4
    assert all(b >= a for a, b in zip(other, other[1:]))
    assert all(c == 11025 for p, _, c, _ in res if p == SCRIPT)


def test_mediaclock_discontinuity_reanchors(monkeypatch):
    monkeypatch.setattr(sys, "stderr", types.SimpleNamespace(buffer=io.BytesIO()))
    c = MediaClock()
    assert c.stamp(VIDEO, 0, 10.0) == pytest.approx(10.0)
    c.stamp(VIDEO, 40, 10.04)
    out = c.stamp(VIDEO, 40 + 60000, 10.08)  # source jumped 60 s
    assert out == pytest.approx(10.08)
    assert c.resyncs == 1


# --- audio trailer clock -----------------------------------------------------


def av_tags(n):
    """25 fps video + 16 kHz AAC (1024 samples = 64 ms) in real time."""
    tags = [(VIDEO, 40 * k) for k in range(n)] + [
        (AUDIO, 64 * j) for j in range(n * 40 // 64)
    ]
    tags.sort(key=lambda t: (t[1], t[0]))
    return tags, [t / 1000 for _, t in tags]


def test_audio_clock_default_unchanged(monkeypatch):
    tags, arrivals = av_tags(400)  # 16 s, so script tags get injected
    for mode in ("wall", "media"):
        res, _ = run(tags, arrivals, mode, monkeypatch)
        assert {c for p, _, c, _ in res if p != VIDEO} == {11025}
        assert {c for p, _, c, _ in res if p == VIDEO} == {90000}


def test_audio_clock_16000_media(monkeypatch):
    tags, arrivals = av_tags(400)
    res, log = run(tags, arrivals, "media", monkeypatch, audio_clock=16000)
    assert "resync" not in log
    assert {c for p, _, c, _ in res if p != VIDEO} == {16000}
    assert {c for p, _, c, _ in res if p == VIDEO} == {90000}
    a = [t for p, _, _, t in res if p == AUDIO]
    d = [y - x for x, y in zip(a, a[1:])]
    assert all(1023 <= x <= 1025 for x in d), (min(d), max(d))  # 1024 samples/frame
    other = [t for p, _, _, t in res if p == SCRIPT]
    assert len(other) >= 3 and all(y >= x for x, y in zip(other, other[1:]))
    # Video unaffected: 40 ms = 3600 ticks.
    assert all(38 <= x <= 42 for x in gaps_ms(video_secs(res)))


def test_audio_clock_out_of_range(monkeypatch):
    with pytest.raises(ValueError):
        run([(VIDEO, 0)], [0.0], "media", monkeypatch, audio_clock=1 << 24)


# --- proxy command lines -----------------------------------------------------


def make_cam(*options):
    parser = argparse.ArgumentParser()
    RTSPCam.add_parser(parser)
    cam = RTSPCam.__new__(RTSPCam)
    cam.args = parser.parse_args(["--source", "rtsp://192.0.2.10/main", *options])
    cam.logger = logging.getLogger("test")
    cam._ffmpeg_handles = {}
    cam._stream_meta = {}
    cam._streams = {}
    cam._needs_flv_timestamps = False
    cam._msg_id = 0
    cam._init_time = time.time()
    return cam


async def spawn(cam, monkeypatch):
    commands = []

    class Process:
        def __init__(self, command, **kwargs):
            commands.append(command)
            self.pid = 5555

        def poll(self):
            return None

    async def source(_):
        return "rtsp://192.0.2.10/main"

    cam.get_stream_source = source
    monkeypatch.setattr("unifi.cams.base.subprocess.check_output", lambda _: b"")
    monkeypatch.setattr("unifi.cams.base.subprocess.Popen", Process)
    await cam.start_video_stream("video1", "HI", ("192.0.2.20", 7550))
    cam._ffmpeg_handles.clear()
    return commands[-1]


async def test_defaults_add_no_clock_sync_flags(monkeypatch):
    cmd = await spawn(make_cam(), monkeypatch)
    assert "--trailer-clock" not in cmd
    assert "--audio-clock" not in cmd
    assert shlex.split(cmd)[-5:-3] == ["unifi.clock_sync", "|"]


async def test_cmd_has_flag_only_in_media(monkeypatch):
    cam = make_cam("--trailer-clock", "media")
    cmd = await spawn(cam, monkeypatch)
    assert cmd.count("unifi.clock_sync") == 1
    assert "--trailer-clock media | nc" in cmd


async def test_audio_clock_flag_only_when_set(monkeypatch):
    cam = make_cam("--trailer-clock", "media", "--audio-trailer-clock", "11025")
    assert "--audio-clock" not in await spawn(cam, monkeypatch)
    cam = make_cam("--trailer-clock", "media", "--audio-trailer-clock", "16000")
    cmd = await spawn(cam, monkeypatch)
    assert "--trailer-clock media --audio-clock 16000 | nc" in cmd


async def test_write_timestamps_still_precedes_clock_flags(monkeypatch):
    cam = make_cam("--trailer-clock", "media")
    cam._needs_flv_timestamps = True
    cmd = await spawn(cam, monkeypatch)
    assert "--write-timestamps --trailer-clock media | nc" in cmd
