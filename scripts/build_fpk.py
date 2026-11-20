#!/usr/bin/env python3
"""Build a deterministic fnOS FPK archive without requiring fnpack on Windows."""

import hashlib
import io
import re
import struct
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_OUTPUT = ROOT / "AVSubscribe-app.tgz"
FPK_OUTPUT = ROOT / "AVSubscribe.fpk"
MANIFEST_FILE = ROOT / "manifest"
ICON_DIR = ROOT / "assets"


def bump_version(version):
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise ValueError(f"Unsupported manifest version: {version}")
    major, minor, patch = (int(part) for part in match.groups())
    minor_increment, patch = divmod(patch + 1, 100)
    return f"{major}.{minor + minor_increment}.{patch}"


def png_icon(size):
    path = ICON_DIR / f"app-icon-{size}.png"
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 24:
        raise ValueError(f"Invalid PNG icon: {path}")
    width, height = struct.unpack(">II", data[16:24])
    if (width, height) != (size, size):
        raise ValueError(f"Expected a {size}x{size} PNG icon at {path}, got {width}x{height}")
    return data


def tar_bytes(entries, directories=()):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        for directory in sorted(directories):
            info = tarfile.TarInfo(directory.rstrip("/") + "/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = 0
            archive.addfile(info)
        for name, data, mode in sorted(entries, key=lambda item: item[0]):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def versioned_app_asset(relative, data, version):
    if relative == "www/version.js":
        return data.replace(b"__APP_VERSION__", version.encode("ascii"))
    return data


def main():
    source_manifest = MANIFEST_FILE.read_text(encoding="utf-8").rstrip()
    version_match = re.search(r"(?m)^version\s*=\s*(\d+\.\d+\.\d+)\s*$", source_manifest)
    if not version_match:
        raise ValueError("manifest is missing a semantic version")
    version = bump_version(version_match.group(1))

    app_entries = []
    for path in (ROOT / "app").rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in (".pyc",):
            relative = path.relative_to(ROOT / "app").as_posix()
            mode = 0o755 if relative in {"bin/app.py", "ui/index.cgi"} else 0o644
            app_entries.append((relative, versioned_app_asset(relative, path.read_bytes(), version), mode))
    app_entries.extend([
        ("ui/images/icon_64.png", png_icon(64), 0o644),
        ("ui/images/icon_256.png", png_icon(256), 0o644),
    ])
    app_dirs = [path.relative_to(ROOT / "app").as_posix() for path in (ROOT / "app").rglob("*") if path.is_dir() and "__pycache__" not in path.parts]
    app_dirs.append("ui/images")
    app_tgz = tar_bytes(app_entries, app_dirs)
    APP_OUTPUT.write_bytes(app_tgz)

    icon = png_icon(256)
    mini_icon = png_icon(64)
    source_manifest = source_manifest[:version_match.start(1)] + version + source_manifest[version_match.end(1):]
    packaged_manifest = source_manifest + f"\nchecksum              = {hashlib.md5(app_tgz).hexdigest()}\n"
    root_entries = [("app.tgz", app_tgz, 0o644), ("manifest", packaged_manifest.encode(), 0o644),
                    ("ICON.PNG", mini_icon, 0o644), ("ICON_256.PNG", icon, 0o644)]
    for path in (ROOT / "config").rglob("*"):
        if path.is_file():
            root_entries.append((path.relative_to(ROOT).as_posix(), path.read_bytes(), 0o644))
    for path in (ROOT / "cmd").rglob("*"):
        if path.is_file():
            root_entries.append((path.relative_to(ROOT).as_posix(), path.read_bytes(), 0o755))
    directories = ["cmd", "config"]
    directories.extend(path.relative_to(ROOT).as_posix() for path in (ROOT / "config").rglob("*") if path.is_dir())
    FPK_OUTPUT.write_bytes(tar_bytes(root_entries, directories))
    MANIFEST_FILE.write_text(source_manifest + "\n", encoding="utf-8")
    print(f"Built {FPK_OUTPUT.name} version {version} ({FPK_OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
