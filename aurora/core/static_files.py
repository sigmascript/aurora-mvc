"""Internal ASGI static-file delivery for configured public directories.

Static assets bypass application middleware and sessions. Opened files are
validated against the configured root, including when filesystem links change.
"""

import asyncio
import mimetypes
import os
import stat
from pathlib import Path


def _open_static_file(root: Path, parts: list[str]):
    """Open a regular file without following replaceable links on POSIX.

    Each component is resolved relative to an already-open directory handle,
    preventing pathname substitution between validation and opening.
    """
    if os.name == "nt":
        return _open_windows_file(root, parts)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise OSError("Secure static-file opening is unavailable")

    fd = os.open(root, flags | directory | nofollow)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, flags | directory | nofollow, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(parts[-1], flags | nofollow | getattr(os, "O_NONBLOCK", 0), dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise OSError("Static asset is not a regular file")
            return os.fdopen(file_fd, "rb")
        except BaseException:
            os.close(file_fd)
            raise
    finally:
        os.close(fd)


def _open_windows_file(root: Path, parts: list[str]):
    """Verify the final Windows file handle, not a pre-open pathname."""
    import ctypes
    import msvcrt

    candidate = root.joinpath(*parts)
    file = candidate.open("rb")
    try:
        handle = msvcrt.get_osfhandle(file.fileno())
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_path = kernel32.GetFinalPathNameByHandleW
        get_path.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        get_path.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_path(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise OSError("Cannot verify opened static file")

        def normalize(path):
            path = str(path)
            if path.startswith("\\\\?\\"):
                path = path[4:]
            return os.path.normcase(os.path.abspath(path))

        actual = normalize(buffer.value)
        base = normalize(root.resolve())
        if os.path.commonpath((actual, base)) != base:
            raise OSError("Static asset is outside the public directory")
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise OSError("Static asset is not a regular file")
        return file
    except BaseException:
        file.close()
        raise


async def serve_static_file(scope, send, *, root: Path, url_prefix: str):
    """Serve configured public files in bounded chunks, never outside root."""
    method = scope.get("method", "GET")
    # ASGI scope["path"] is already percent-decoded by the server.
    path = scope["path"][len(url_prefix) + 1:]
    parts = path.split("/")
    valid = bool(parts) and all(
        part not in {"", ".", ".."} and not part.startswith(".")
        and "\\" not in part and "\x00" not in part for part in parts
    )

    if not valid:
        status, content = 404, b"Not Found"
    elif method not in {"GET", "HEAD"}:
        status, content = 405, b"Method Not Allowed"
    else:
        # No response has started yet: open/metadata failures become 404.
        try:
            file = await asyncio.to_thread(_open_static_file, root, parts)
        except OSError:
            status, content = 404, b"Not Found"
        else:
            try:
                size = os.fstat(file.fileno()).st_size
                content_type = (mimetypes.guess_type(parts[-1])[0]
                                or "application/octet-stream").encode("ascii")
                headers = [(b"content-type", content_type),
                           (b"content-length", str(size).encode("ascii")),
                           (b"x-content-type-options", b"nosniff")]
                await send({"type": "http.response.start", "status": 200, "headers": headers})
                if method == "HEAD" or size == 0:
                    await send({"type": "http.response.body", "body": b""})
                else:
                    chunk = await asyncio.to_thread(file.read, 64 * 1024)
                    while chunk:
                        next_chunk = await asyncio.to_thread(file.read, 64 * 1024)
                        await send({"type": "http.response.body", "body": chunk,
                                    "more_body": bool(next_chunk)})
                        chunk = next_chunk
                return
            finally:
                await asyncio.to_thread(file.close)

    headers = [(b"content-type", b"text/plain; charset=utf-8"),
               (b"content-length", str(len(content)).encode("ascii")),
               (b"x-content-type-options", b"nosniff")]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": b"" if method == "HEAD" else content})
