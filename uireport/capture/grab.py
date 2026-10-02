"""Screen grabbing. Owner: capture builder (package A)."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from PySide6.QtGui import QImage

from ..geometry import IntRect
from . import winapi
from .monitors import virtual_screen_rect

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
BI_RGB = 0
DIB_RGB_COLORS = 0


class GrabError(RuntimeError):
    """The desktop could not be captured (locked session, secure desktop, GDI failure)."""


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True) if winapi.IS_WINDOWS else None
if _gdi32 is not None:
    _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi32.CreateCompatibleDC.restype = wintypes.HDC
    _gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.POINTER(_BITMAPINFO), wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
    ]
    _gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _gdi32.SelectObject.restype = wintypes.HGDIOBJ
    _gdi32.BitBlt.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
    ]
    _gdi32.BitBlt.restype = wintypes.BOOL
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi32.DeleteObject.restype = wintypes.BOOL
    _gdi32.DeleteDC.argtypes = [wintypes.HDC]
    _gdi32.DeleteDC.restype = wintypes.BOOL
    _gdi32.GdiFlush.argtypes = []
    _gdi32.GdiFlush.restype = wintypes.BOOL
if winapi.user32 is not None:
    winapi.user32.GetDC.argtypes = [wintypes.HWND]
    winapi.user32.GetDC.restype = wintypes.HDC
    winapi.user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    winapi.user32.ReleaseDC.restype = ctypes.c_int


def grab_virtual_screen() -> tuple[QImage, IntRect]:
    """Grab the ENTIRE virtual desktop (all monitors) in one go, in physical pixels
    (GDI BitBlt SRCCOPY | CAPTUREBLT from the desktop DC).

    Fast path: BitBlt straight into a top-down 32-bit DIB section (no GetDIBits pass),
    then exactly ONE memcpy into a QImage that owns its pixels (QImage.copy()). The
    alpha byte GDI leaves undefined is ignored because the QImage is Format_RGB32.

    Returns (image, origin_rect): `origin_rect` is the virtual-screen rectangle the image
    covers (x/y may be negative), and image.size() == (origin_rect.w, origin_rect.h).
    The returned QImage has devicePixelRatio() == 1.0 (its width()/height() are physical
    pixels) and is a self-owned copy (not backed by a freed GDI buffer).
    Raises GrabError on failure."""
    if _gdi32 is None or winapi.user32 is None:
        raise GrabError("Screen capture is only available on Windows")
    rect = virtual_screen_rect()
    if rect.is_empty:
        raise GrabError("Could not determine the virtual screen size")
    w, h = rect.w, rect.h

    bmi = _BITMAPINFO()
    hdr = bmi.bmiHeader
    hdr.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    hdr.biWidth = w
    hdr.biHeight = -h  # negative = top-down rows
    hdr.biPlanes = 1
    hdr.biBitCount = 32
    hdr.biCompression = BI_RGB

    hdc_screen = winapi.user32.GetDC(None)
    if not hdc_screen:
        raise GrabError("GetDC(desktop) failed")
    hdc_mem = None
    hbmp = None
    old = None
    try:
        hdc_mem = _gdi32.CreateCompatibleDC(hdc_screen)
        if not hdc_mem:
            raise GrabError("CreateCompatibleDC failed")
        bits = ctypes.c_void_p()
        hbmp = _gdi32.CreateDIBSection(hdc_screen, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        if not hbmp or not bits.value:
            raise GrabError(f"CreateDIBSection failed for {w}x{h}")
        old = _gdi32.SelectObject(hdc_mem, hbmp)
        if not _gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, rect.x, rect.y, SRCCOPY | CAPTUREBLT):
            raise GrabError("BitBlt from the desktop failed (locked screen or secure desktop?)")
        _gdi32.GdiFlush()
        stride = w * 4
        view = (ctypes.c_ubyte * (stride * h)).from_address(bits.value)
        wrapped = QImage(view, w, h, stride, QImage.Format.Format_RGB32)
        image = wrapped.copy()  # detach from the DIB memory (freed below)
        del wrapped, view
    finally:
        if hdc_mem and old:
            _gdi32.SelectObject(hdc_mem, old)
        if hbmp:
            _gdi32.DeleteObject(hbmp)
        if hdc_mem:
            _gdi32.DeleteDC(hdc_mem)
        winapi.user32.ReleaseDC(None, hdc_screen)
    if image.isNull():
        raise GrabError("The captured image is empty")
    image.setDevicePixelRatio(1.0)
    return image, rect


def crop_image(frozen: QImage, frozen_origin: IntRect, rect: IntRect) -> QImage:
    """Copy `rect` (virtual-screen physical coordinates) out of `frozen` (which covers
    `frozen_origin`). `rect` is first clamped to the frozen image. Returns a detached
    QImage, dpr 1.0, exactly rect.w x rect.h pixels, never scaled.

    Raises ValueError when `rect` does not intersect the frozen image."""
    bounds = IntRect(frozen_origin.x, frozen_origin.y, frozen.width(), frozen.height())
    inter = rect.intersection(bounds)
    if inter is None:
        raise ValueError("The selection lies outside the captured screen")
    out = frozen.copy(inter.x - bounds.x, inter.y - bounds.y, inter.w, inter.h)
    out.setDevicePixelRatio(1.0)
    return out
