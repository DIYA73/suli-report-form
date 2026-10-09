# -*- coding: utf-8 -*-
import pytest

import uploads


@pytest.mark.parametrize("data,expected", [
    (b"\xff\xd8\xff\xe0abc", ("jpg", "image/jpeg")),
    (b"\x89PNG\r\n\x1a\nabc", ("png", "image/png")),
    (b"GIF89a....", ("gif", "image/gif")),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", ("webp", "image/webp")),
    (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00", ("heic", "image/heic")),
    (b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00", ("heic", "image/heic")),
    (b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00", ("avif", "image/avif")),
])
def test_sniff_image_accepts_real_images(data, expected):
    assert uploads.sniff_image(data) == expected


@pytest.mark.parametrize("data", [
    b"", b"<html><script>alert(1)</script></html>", b"<svg xmlns='http://www.w3.org/2000/svg'/>",
    b"just some text", b"RIFF\x00\x00\x00\x00WAVEfmt ", b"\x1a\x45\xdf\xa3",
])
def test_sniff_image_rejects_everything_else(data):
    assert uploads.sniff_image(data) is None


@pytest.mark.parametrize("data,expected", [
    (b"\x1a\x45\xdf\xa3abc", ("webm", "audio/webm")),
    (b"OggS\x00\x02", ("ogg", "audio/ogg")),
    (b"RIFF\x00\x00\x00\x00WAVEfmt ", ("wav", "audio/wav")),
    (b"\x00\x00\x00\x1cftypM4A \x00\x00\x00\x00", ("m4a", "audio/mp4")),
    (b"\x00\x00\x00\x20ftypisom\x00\x00\x00\x00", ("m4a", "audio/mp4")),
    (b"ID3\x04\x00", ("mp3", "audio/mpeg")),
    (b"\xff\xfb\x90\x00", ("mp3", "audio/mpeg")),
    (b"\xff\xf1\x50\x80", ("aac", "audio/aac")),
])
def test_sniff_audio_accepts_real_audio(data, expected):
    assert uploads.sniff_audio(data) == expected


@pytest.mark.parametrize("data", [
    b"", b"<html></html>", b"\xff\xd8\xff\xe0abc", b"\x89PNG\r\n\x1a\n",
    b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00",  # a HEIC photo is not audio
])
def test_sniff_audio_rejects_everything_else(data):
    assert uploads.sniff_audio(data) is None
