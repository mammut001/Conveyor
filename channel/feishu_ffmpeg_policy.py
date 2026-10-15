"""Restricted FFmpeg command for untrusted Feishu Opus payloads.

Force a raw Opus demuxer and allow only local file/pipe protocols; playlist,
network and concat demuxers must not be selected by input bytes or extension.
"""
from __future__ import annotations


def opus_to_ogg_command(source, destination):
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-protocol_whitelist", "file,pipe", "-f", "ogg", "-i", str(source),
        "-map", "0:a:0", "-vn", "-c:a", "libopus", "-b:a", "32k",
        "-f", "ogg", str(destination),
    ]
