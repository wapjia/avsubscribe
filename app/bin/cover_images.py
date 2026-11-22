"""Fetch public cover images and read the app's local image files."""

import http.client
import io
import ipaddress
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path


MAX_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 24_000_000
POSTER_CROP_VERSION = 2
POSTER_WIDTH_NUMERATOR = 47
POSTER_WIDTH_DENOMINATOR = 100
IMAGE_TYPES = {
    ".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
}


class ImageHTTPError(ValueError):
    def __init__(self, status):
        super().__init__(f"图片下载失败（HTTP {status}）。")
        self.status = status


def image_extension(data):
    if len(data) > MAX_BYTES:
        raise ValueError("封面超过 12 MiB。")
    if len(data) >= 12 and data.startswith(b"\xff\xd8\xff") and data.rstrip(b" \t\r\n").endswith(b"\xff\xd9"):
        return ".jpg"
    if len(data) >= 45 and data.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR") and data.endswith(b"\x00\x00\x00\x00IEND\xaeB\x60\x82"):
        return ".png"
    if len(data) >= 20 and data.startswith(b"RIFF") and data[8:16] in (b"WEBPVP8 ", b"WEBPVP8L", b"WEBPVP8X") and int.from_bytes(data[4:8], "little") + 8 == len(data):
        return ".webp"
    if len(data) >= 15 and data.startswith((b"GIF87a", b"GIF89a")) and data.endswith(b";"):
        return ".gif"
    raise ValueError("封面图片格式无效。")


def crop_poster(image, extension):
    """Keep the rightmost 47% of the displayed cover at full height."""
    formats = {".jpg": "JPEG", ".png": "PNG", ".webp": "WEBP", ".gif": "GIF"}
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return crop_poster_ffmpeg(image, extension)
    try:
        with Image.open(io.BytesIO(image), formats=[formats[extension]]) as original:
            if original.width * original.height > MAX_PIXELS:
                raise ValueError("封面像素数超过裁剪上限。")
            oriented = ImageOps.exif_transpose(original)
            if oriented.width < 2:
                raise ValueError("封面宽度不足，无法裁剪。")
            width = (oriented.width * POSTER_WIDTH_NUMERATOR + POSTER_WIDTH_DENOMINATOR // 2) // POSTER_WIDTH_DENOMINATOR
            poster = oriented.crop((oriented.width - max(1, width), 0, oriented.width, oriented.height))
            output = io.BytesIO()
            options = {"quality": 95} if extension in (".jpg", ".webp") else {}
            poster.save(output, format=formats[extension], **options)
    except Image.DecompressionBombError as error:
        raise ValueError("封面像素数超过裁剪上限。") from error
    return checked_poster(output.getvalue(), extension)


def crop_poster_ffmpeg(image, extension):
    executable = shutil.which("ffmpeg") or shutil.which("/usr/trim/bin/ffmpeg")
    if not executable:
        raise ValueError("找不到 FFmpeg 或 Pillow，无法裁剪海报。")
    demuxers = {".jpg": "jpeg_pipe", ".png": "png_pipe", ".webp": "webp_pipe", ".gif": "gif"}
    encoders = {".jpg": "mjpeg", ".png": "png", ".webp": "libwebp", ".gif": "gif"}
    input_options = [
        executable, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-protocol_whitelist", "file,pipe", "-max_pixels", str(MAX_PIXELS),
        "-threads", "1", "-f", demuxers[extension],
    ]
    rounded_width = f"max(1\\,floor((iw*{POSTER_WIDTH_NUMERATOR}+{POSTER_WIDTH_DENOMINATOR // 2})/{POSTER_WIDTH_DENOMINATOR}))"
    output_options = [
        "-filter_threads", "1", "-vf", f"crop={rounded_width}:ih:iw-ow:0:exact=1,setsar=1",
        "-frames:v", "1", "-threads", "1", "-c:v", encoders[extension],
    ]
    if extension == ".jpg":
        output_options.extend(("-q:v", "2"))
    muxer = {".gif": "gif", ".webp": "webp"}.get(extension, "image2pipe")
    output_options.extend(("-fs", str(MAX_BYTES), "-f", muxer, "pipe:1"))
    try:
        with tempfile.TemporaryDirectory(prefix="avsubscribe-poster-") as directory:
            source = Path(directory) / ("cover" + extension)
            source.write_bytes(image)
            command = [*input_options, "-i", str(source), *output_options]
            result = subprocess.run(command, capture_output=True, timeout=20, check=True)
    except (subprocess.SubprocessError, OSError) as error:
        raise ValueError("FFmpeg 无法裁剪封面。") from error
    return checked_poster(result.stdout, extension)


def checked_poster(image, extension):
    if image_extension(image) != extension:
        raise ValueError("海报格式与封面不一致。")
    return image


def public_image_address(url):
    if not isinstance(url, str) or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ValueError("封面网址无效。")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("封面网址必须为公开的 HTTP(S) 地址。")
    host = parsed.hostname.encode("idna").decode("ascii")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for _family, _type, _protocol, _canonname, address in addresses:
        ip = ipaddress.ip_address(address[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise ValueError("封面网址指向非公开地址。")
    if not addresses:
        raise ValueError("无法解析封面网址。")
    return parsed, host, port, list(dict.fromkeys(info[-1][0] for info in addresses))


def fetch(url):
    deadline = time.monotonic() + 15
    for _ in range(5):
        parsed, host, port, addresses = public_image_address(url)
        connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(host, port, timeout=5)

        def connect_public(_address, timeout, source_address):
            # Preserve the URL host for Host and TLS verification, but use a vetted IP.
            for address in addresses:
                if time.monotonic() >= deadline:
                    raise TimeoutError("封面下载超时。")
                try:
                    return socket.create_connection((address, port), timeout, source_address)
                except OSError as error:
                    last_error = error
            raise last_error

        connection._create_connection = connect_public
        target = urllib.parse.quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if parsed.query:
            target += "?" + urllib.parse.quote(parsed.query, safe="/?%:@!$&'()*+,;=-._~")
        try:
            connection.request("GET", target, headers={"Accept": "image/jpeg, image/png, image/webp, image/gif"})
            with connection.getresponse() as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise ValueError("封面跳转缺少地址。")
                    url = urllib.parse.urljoin(url, location)
                    continue
                if response.status != 200:
                    raise ImageHTTPError(response.status)
                length = response.getheader("Content-Length", "")
                if length.isdecimal() and int(length) > MAX_BYTES:
                    raise ValueError("封面超过 12 MiB。")
                data = bytearray()
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("封面下载超时。")
                    chunk = response.read1(min(65536, MAX_BYTES + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError("封面超过 12 MiB。")
                image = bytes(data)
                return image_extension(image), image
        finally:
            connection.close()
    raise ValueError("封面跳转次数过多。")


def read_file(path):
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("封面文件不能是符号链接。")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    if os.open in os.supports_dir_fd:
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(path.name, flags, dir_fd=parent)
        finally:
            os.close(parent)
    else:
        fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        file_stat = os.fstat(stream.fileno())
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > MAX_BYTES:
            raise ValueError("封面文件无效。")
        image = stream.read(MAX_BYTES + 1)
    if image_extension(image) != path.suffix:
        raise ValueError("封面扩展名与实际格式不一致。")
    return image
