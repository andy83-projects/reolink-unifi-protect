"""
Helper program to inject absolute wall clock time into FLV stream for recordings
"""

import argparse
import struct
import sys
import time

from flvlib3.astypes import FLVObject
from flvlib3.primitives import make_ui8, make_ui32
from flvlib3.tags import create_script_tag


def read_bytes(source, num_bytes):
    read_bytes = 0
    buf = b""
    while read_bytes < num_bytes:
        d_in = source.read(num_bytes - read_bytes)
        if d_in:
            read_bytes += len(d_in)
            buf += d_in
        else:
            return buf
    return buf


def write(data):
    sys.stdout.buffer.write(data)


def write_log(data):
    sys.stderr.buffer.write(f"{data}\n".encode())


DEFAULT_AUDIO_CLOCK = 11025
_audio_clock = DEFAULT_AUDIO_CLOCK


def write_timestamp_trailer(is_packet, ts):
    # 16-byte inter-tag trailer: one zero byte, a 3-byte clock rate
    # (90000 for video tags, 11025 for everything else), 8 padding bytes,
    # then elapsed wall-clock in TICKS OF THAT CLOCK. The old fixed
    # ts*100000 made Protect's ms derive audio wall-clocks running 9x
    # fast and video 1.11x fast; once the drift crossed its 1000 ms
    # threshold it dropped frames (InNetLiveFLVStream::FeedData FAILED),
    # starving live view and the recorder.
    #
    # --audio-clock sets the non-video clock (default 11025). ms serves the
    # audio trailer ticks to RTSP clients as RTP timestamps, so
    # with 16 kHz AAC it must be 16000 or the audio RTP/RTCP clock runs at
    # 0.69x the rate the SDP advertises. Script tags share the same clock so
    # the non-video timeline stays in one unit.
    write(make_ui8(0))
    if is_packet:
        clock = 90000
        write(bytes([1, 95, 144, 0, 0, 0, 0, 0, 0, 0, 0]))
    else:
        clock = _audio_clock
        write(clock.to_bytes(3, "big") + bytes(8))

    write(make_ui32(int(ts * clock) & 0xFFFFFFFF))


class MediaClock:
    """Trailer clock from the FLV tags' own media timestamps (--trailer-clock media).

    The default (wall) trailer is the time the tag was READ from ffmpeg's
    pipe. Upstream delivery is bursty (TCP-interleaved RTSP, restream proxies), so
    wall stamps arrive as near-duplicate pairs followed by 100-300 ms gaps,
    and Protect's ms serves those stamps to RTSP clients as the
    RTP timestamps. Media mode keeps the camera's frame spacing and slews
    gently toward wall clock, so ms's trailer-vs-wall drift stays bounded
    (it drops frames past ~1000 ms, see write_timestamp_trailer):

    - err = wall - out. err > RESYNC_S (a real stall): jump forward once.
    - else, video tags only: off += clamp(err, +-cap), cap = SLEW of the
      tag's media interval (DRAIN when media leads by > DRAIN_S, e.g. a
      startup GOP burst). Never jumps backward; out is monotonic per kind.
    """

    RESYNC_S = 0.6
    DRAIN_S = 0.25
    SLEW = 0.02
    DRAIN = 0.25
    MAX_STEP_MS = 5000

    def __init__(self):
        self.prev_ts = None  # last media tag's raw FLV timestamp (ms)
        self.m = 0.0  # unwrapped media time (s) of the last media tag
        self.off = 0.0
        self.prev_video_m = None
        self.last_out = {}  # kind -> last trailer value written
        self.resyncs = 0

    def _monotonic(self, kind, out):
        last = self.last_out.get(kind)
        if last is not None and out < last:
            out = last
        self.last_out[kind] = out
        return out

    def stamp(self, packet_type, ts_ms, wall):
        """Trailer value (seconds) for a tag read at `wall` (now - start)."""
        if packet_type not in (8, 9):
            # Script tags (onMetaData) carry no media time: before the first
            # media tag use wall; afterwards the current media position.
            out = wall if self.prev_ts is None else self.m + self.off
            return self._monotonic("other", out)

        is_video = packet_type == 9
        if self.prev_ts is None:
            self.m = 0.0
            self.off = wall  # anchor on the first audio/video tag
        else:
            d = (ts_ms - self.prev_ts) & 0xFFFFFFFF
            if d >= 0x80000000:
                d -= 0x100000000
            self.m += d / 1000.0
            if abs(d) > self.MAX_STEP_MS:
                # Source timestamp discontinuity: re-anchor at wall.
                self.off = wall - self.m
                self.prev_video_m = None
                self.resyncs += 1
                write_log(f"clock_sync: media clock re-anchor ({d} ms step)")
        self.prev_ts = ts_ms

        out = self.m + self.off
        err = wall - out
        if err > self.RESYNC_S:
            self.off += err
            out = wall
            self.resyncs += 1
            write_log(f"clock_sync: media clock resync +{err:.3f}s")
        elif is_video and self.prev_video_m is not None:
            dt = min(max(self.m - self.prev_video_m, 0.001), 1.0)
            cap = (self.DRAIN if err < -self.DRAIN_S else self.SLEW) * dt
            adj = max(-cap, min(cap, err))
            self.off += adj
            out += adj
        if is_video:
            self.prev_video_m = self.m
        return self._monotonic("video" if is_video else "audio", out)

    def other(self, out):
        """Trailer for an injected script tag (onClockSync / onMpma)."""
        return self._monotonic("other", out)


def main(args):
    global _audio_clock
    _audio_clock = int(getattr(args, "audio_clock", None) or DEFAULT_AUDIO_CLOCK)
    if not 0 < _audio_clock < 1 << 24:
        raise ValueError(f"--audio-clock out of range: {_audio_clock}")
    source = sys.stdin.buffer
    clock = MediaClock() if getattr(args, "trailer_clock", "wall") == "media" else None

    header = read_bytes(source, 3)

    if header != b"FLV":
        print("Not a valid FLV file")
        return
    write(header)

    # Skip rest of FLV header
    write(read_bytes(source, 1))
    read_bytes(source, 1)
    # Write custom bitmask for FLV type
    write(make_ui8(7))
    write(read_bytes(source, 4))

    # Tag 0 previous size
    write(read_bytes(source, 4))

    last_ts = time.time()
    start = time.time()
    i = 0
    while True:
        # Packet structure from Wikipedia:
        #
        # Size of previous packet	uint32_be	0	For first packet set to NULL
        #
        # Packet Type	uint8	18	For first packet set to AMF Metadata
        # Payload Size	uint24_be	varies	Size of packet data only
        # Timestamp Lower	uint24_be	0	For first packet set to NULL
        # Timestamp Upper	uint8	0	Extension to create a uint32_be value
        # Stream ID	uint24_be	0	For first stream of same type set to NULL
        #
        # Payload Data	freeform	varies	Data as defined by packet type

        header = read_bytes(source, 12)
        if len(header) != 12:
            write(header)
            return

        # Packet type
        packet_type = header[0]

        # Get payload size to know how many bytes to read
        high, low = struct.unpack(">BH", header[1:4])
        payload_size = (high << 16) + low

        # Get timestamp to inject into clock sync tag
        low_high = header[4:8]
        combined = bytes([low_high[3]]) + low_high[:3]
        timestamp = struct.unpack(">i", combined)[0]

        now = time.time()
        if clock is None:
            tag_ts = other_ts = now - start
        else:
            tag_ts = clock.stamp(packet_type, timestamp, now - start)
            other_ts = clock.other(tag_ts)
        if not last_ts or now - last_ts >= 5:
            last_ts = now
            # Insert a custom packet every so often for time synchronization
            data = FLVObject()
            data["streamClock"] = int(timestamp)
            data["streamClockBase"] = 0
            data["wallClock"] = now * 1000
            packet_to_inject = create_script_tag("onClockSync", data, timestamp)
            write(packet_to_inject)

            # Write 15 byte trailer
            write_timestamp_trailer(False, other_ts)

            # Write mpma tag
            # {'cs': {'cur': 1500000.0,
            #         'max': 1500000.0,
            #         'min': 32000.0},
            #  'm': {'cur': 750000.0,
            #        'max': 1500000.0,
            #        'min': 750000.0},
            #  'r': 0.0,
            #  'sp': {'cur': 1500000.0,
            #         'max': 1500000.0,
            #         'min': 150000.0},
            #  't': 750000.0}

            data = FLVObject()
            data["cs"] = FLVObject()
            data["cs"]["cur"] = 1500000
            data["cs"]["max"] = 1500000
            data["cs"]["min"] = 1500000

            data["m"] = FLVObject()
            data["m"]["cur"] = 1500000
            data["m"]["max"] = 1500000
            data["m"]["min"] = 1500000
            data["r"] = 0

            data["sp"] = FLVObject()
            data["sp"]["cur"] = 1500000
            data["sp"]["max"] = 1500000
            data["sp"]["min"] = 1500000
            data["t"] = 75000.0
            packet_to_inject = create_script_tag("onMpma", data, 0)

            write(packet_to_inject)

            # Write 15 byte trailer
            write_timestamp_trailer(False, other_ts)

            # Write rest of original packet minus previous packet size
            write(header)
            write(read_bytes(source, payload_size))
        else:
            # Write the original packet
            write(header)
            write(read_bytes(source, payload_size))

        # Write previous packet size
        write(read_bytes(source, 3))

        # Write 15 byte trailer
        write_timestamp_trailer(packet_type == 9, tag_ts)
        if clock is not None:
            # Hand each tag to nc as soon as it is stamped: otherwise
            # small frames sit in Python's 8 KB stdout buffer and reach
            # ms in bursts again.
            sys.stdout.buffer.flush()

        # Write mpma tag
        i += 1


def parse_args():
    parser = argparse.ArgumentParser(description="Modify Protect FLV stream")
    parser.add_argument(
        "--write-timestamps",
        action="store_true",
        help="Indicates we should write timestamp in between packets",
    )
    parser.add_argument(
        "--trailer-clock",
        default="wall",
        choices=["wall", "media"],
        help="Trailer timestamps from pipe-read wall clock (default) or from "
        "the FLV tags' own media timestamps, slewed toward wall clock",
    )
    parser.add_argument(
        "--audio-clock",
        default=DEFAULT_AUDIO_CLOCK,
        type=int,
        help="Clock rate (Hz) of non-video trailers; set to the AAC sample "
        "rate (e.g. 16000) so ms serves correct audio RTP timestamps "
        "(default 11025)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    main(parse_args())
