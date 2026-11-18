"""--fw-state-file keeps the version adopted from Protect's push across restarts."""

import argparse
import json
import logging

from unifi import fw_state
from unifi.cams.rtsp import RTSPCam

LOG = logging.getLogger("test")
PUSH = (
    "https://192.168.1.1:7444/internal/update/sav837gw/uvc/firmware/5.5.51/AABBCC00C129"
)


def write(path, version):
    path.write_text(json.dumps({"fw_version": version}))


def test_missing_file_keeps_flag(tmp_path):
    assert fw_state.load(str(tmp_path / "x.json"), "5.4.132", True, LOG) == "5.4.132"


def test_newer_stored_version_wins(tmp_path):
    f = tmp_path / "fw.json"
    write(f, "5.5.51")
    assert fw_state.load(str(f), "5.4.132", True, LOG) == "5.5.51"


def test_bumped_flag_beats_older_stored_version(tmp_path):
    f = tmp_path / "fw.json"
    write(f, "5.5.45")
    assert fw_state.load(str(f), "5.5.51", True, LOG) == "5.5.51"


def test_numeric_not_lexical_compare(tmp_path):
    f = tmp_path / "fw.json"
    write(f, "5.10.1")
    assert fw_state.load(str(f), "5.9.99", True, LOG) == "5.10.1"


def test_wrong_form_is_ignored(tmp_path):
    f = tmp_path / "fw.json"
    write(f, "UVC.S2L.v9.0.0.1.abc.200526.1046")
    assert fw_state.load(str(f), "5.4.132", True, LOG) == "5.4.132"
    write(f, "9.0.0")
    legacy = "UVC.S2L.v4.23.8.67.0eba6e3.200526.1046"
    assert fw_state.load(str(f), legacy, False, LOG) == legacy


def test_corrupt_file_is_ignored(tmp_path):
    f = tmp_path / "fw.json"
    f.write_text("{not json")
    assert fw_state.load(str(f), "5.4.132", True, LOG) == "5.4.132"


def test_save_creates_directory(tmp_path):
    f = tmp_path / "state" / "fw.json"
    fw_state.save(str(f), "5.5.51", LOG)
    assert json.loads(f.read_text()) == {"fw_version": "5.5.51"}


def test_save_failure_does_not_raise(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    fw_state.save(str(blocker / "fw.json"), "5.5.51", LOG)  # parent is a file


async def test_push_is_persisted_and_survives_restart(tmp_path, monkeypatch):
    f = tmp_path / "fw.json"
    cam = RTSPCam.__new__(RTSPCam)
    cam.args = argparse.Namespace(
        fw_version="5.4.132", sysid="0xa598", host="192.0.2.1", fw_state_file=str(f)
    )
    cam.logger = LOG
    await cam.process_upgrade(
        {"functionName": "UpdateFirmwareRequest", "payload": {"uri": PUSH}}
    )
    assert cam.args.fw_version == "5.5.51"
    # "Restart": the flag still says 5.4.132, the file brings back 5.5.51.
    assert fw_state.load(str(f), "5.4.132", True, LOG) == "5.5.51"


async def test_no_state_file_flag_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cam = RTSPCam.__new__(RTSPCam)
    cam.args = argparse.Namespace(
        fw_version="5.4.132", sysid="0xa598", host="192.0.2.1"
    )
    cam.logger = LOG
    await cam.process_upgrade(
        {"functionName": "UpdateFirmwareRequest", "payload": {"uri": PUSH}}
    )
    assert list(tmp_path.iterdir()) == []
