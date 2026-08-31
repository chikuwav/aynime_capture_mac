"""
macOS 版 aynime_capture。

Windows 版（Windows.Graphics.Capture + D3D11 の C++ 実装）と
同じ API を、ScreenCaptureKit で提供する。
import 名を Windows 版と揃えてあるので、呼び出し側は OS を意識しない。
"""

from aynime_capture.session import Session, Snapshot, set_log_handle

__all__ = ["Session", "Snapshot", "set_log_handle"]
