# -*- coding: utf-8 -*-
"""
Identifies what an uploaded file really is from its first bytes ("magic
numbers"), instead of trusting the filename/Content-Type the client sent.

Why this matters: the report form accepts a "photo" and a "voice note" from
anyone on the internet. Without checking, someone could upload an .html or
.svg file as the "photo" - it would be saved with that extension and then
served back from our own domain as text/html (a stored-XSS hole). Here the
extension and content type we store/serve always come from the detected type,
and anything that isn't a real image/audio file is rejected.

Each sniff_* function returns (extension, content_type), or None when the
bytes don't look like a supported file of that kind.
"""

# Brands that mean "this ISO-BMFF file is a HEIF/HEIC photo" (iPhone photos).
_HEIF_BRANDS = {
    b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs",
    b"mif1", b"msf1",
}
_AVIF_BRANDS = {b"avif", b"avis"}


def _ftyp_brand(data):
    """For MP4/QuickTime/HEIF-style containers, returns the 4-byte major
    brand (e.g. b"heic", b"M4A ", b"isom"), else None."""
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return data[8:12]
    return None


def sniff_image(data):
    if data[:3] == b"\xff\xd8\xff":
        return ("jpg", "image/jpeg")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ("png", "image/png")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ("gif", "image/gif")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("webp", "image/webp")
    brand = _ftyp_brand(data)
    if brand in _HEIF_BRANDS:
        return ("heic", "image/heic")
    if brand in _AVIF_BRANDS:
        return ("avif", "image/avif")
    return None


def sniff_audio(data):
    # Chrome/Android/Edge MediaRecorder -> WebM (EBML header)
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return ("webm", "audio/webm")
    # Firefox MediaRecorder -> Ogg
    if data[:4] == b"OggS":
        return ("ogg", "audio/ogg")
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ("wav", "audio/wav")
    # Safari/iOS MediaRecorder -> MP4/AAC (also covers .m4a / .3gp uploads)
    brand = _ftyp_brand(data)
    if brand is not None and brand not in _HEIF_BRANDS and brand not in _AVIF_BRANDS:
        return ("m4a", "audio/mp4")
    if data[:3] == b"ID3":
        return ("mp3", "audio/mpeg")
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        layer = (data[1] >> 1) & 0x03  # 0 = AAC (ADTS), otherwise MP3
        return ("aac", "audio/aac") if layer == 0 else ("mp3", "audio/mpeg")
    return None
