"""
キャプチャセッション（macOS 版）。

Windows 版（Windows.Graphics.Capture + D3D11 の C++ 実装）と同じ意味・同じ
シグネチャの API を、ScreenCaptureKit で提供する。
"""

# std
from typing import Any, Optional
import threading
import time
from collections import deque

# PIL
from PIL import Image

# macOS
import Quartz
import ScreenCaptureKit as SCK
from AppKit import NSApplication
from CoreMedia import (
    CMSampleBufferGetImageBuffer,
    CMSampleBufferGetPresentationTimeStamp,
    CMSampleBufferGetSampleAttachmentsArray,
    CMTimeGetSeconds,
    CMTimeMake,
)
import objc
from Foundation import NSObject

# 非同期な ScreenCaptureKit 呼び出しの待ち時間
_CALLBACK_TIMEOUT_IN_SEC = 8.0

# SCStreamOutputType.screen
_OUTPUT_TYPE_SCREEN = 0

# SCFrameStatus.complete
_FRAME_STATUS_COMPLETE = 0

# ウィンドウの位置・サイズを監視する間隔
# NOTE
#   Windows 版はウィンドウそのものをキャプチャ対象にするので、
#   ウィンドウを動かしてもキャプチャは勝手に追従する。
#   macOS 版はディスプレイ上の矩形を切り出す方式なので、
#   同じ挙動にするために矩形の変化を自分で監視して追従する。
_WINDOW_TRACK_INTERVAL_IN_SEC = 0.5

# キャプチャの上限フレームレート
# NOTE
#   Windows 版はウィンドウの更新があった時だけフレームが届く。
#   ScreenCaptureKit も内容に変化がないフレームは complete 以外の状態で届くので、
#   それを捨てることで同じ挙動になる。
#   ここでの指定は、その上での上限値。
_MAX_FRAME_RATE = 60


def set_log_handle(handle: int) -> None:
    """
    ログ出力を設定する。

    NOTE
        Windows 版は C++ 実装のログを匿名パイプで Python 側へ流すための API。
        macOS 版は Python 実装なので、この配管そのものが不要。
    """
    return None


def _wait_async(start, timeout: float = _CALLBACK_TIMEOUT_IN_SEC):
    """
    非同期な ScreenCaptureKit の呼び出しを同期的に扱う。

    NOTE
        完了ハンドラは dispatch queue 上で呼ばれるので、
        メインのランループを回す必要がない。
        Tk が mainloop を握っていても干渉しない。
    """
    done = threading.Event()
    box = []

    def handler(*args):
        box.append(args)
        done.set()

    start(handler)
    if not done.wait(timeout):
        return None
    return box[0] if box else None


def _resolve_optimal_frame_size(
    source_width: int,
    source_height: int,
    max_width: Optional[int],
    max_height: Optional[int],
) -> tuple[int, int]:
    """
    最適なフレームサイズを計算する。

    NOTE
        縮小はするが、拡大はしない。
        画像全体が max_width / max_height の枠内に収まるようにする。
        指定がなければ等倍。
    """
    width_scale = 1.0
    if max_width is not None:
        width_scale = min(1.0, max_width / source_width)
    height_scale = 1.0
    if max_height is not None:
        height_scale = min(1.0, max_height / source_height)
    merged_scale = min(width_scale, height_scale)
    return (
        max(1, round(merged_scale * source_width)),
        max(1, round(merged_scale * source_height)),
    )


def _copy_pixels(pixel_buffer) -> Optional[tuple[int, int, int, bytes]]:
    """
    CVPixelBuffer の中身を (幅, 高さ, ストライド, バイト列) として複製する。

    NOTE
        ScreenCaptureKit のバッファはプールの使い回しなので、
        参照を持ち越すとプールを枯渇させて stream が止まる。
        受け取ったその場で複製しなければならない。
    """
    Quartz.CVPixelBufferLockBaseAddress(pixel_buffer, 1)
    try:
        width = Quartz.CVPixelBufferGetWidth(pixel_buffer)
        height = Quartz.CVPixelBufferGetHeight(pixel_buffer)
        stride = Quartz.CVPixelBufferGetBytesPerRow(pixel_buffer)
        base = Quartz.CVPixelBufferGetBaseAddress(pixel_buffer)
        if base is None:
            return None
        return width, height, stride, bytes(base.as_buffer(stride * height))
    finally:
        Quartz.CVPixelBufferUnlockBaseAddress(pixel_buffer, 1)


def _to_bgr_bytes(frame: tuple[int, int, int, bytes]) -> tuple[int, int, bytes]:
    """
    32BGRA のバイト列を、隙間なく詰めた BGR のバイト列に変換する。

    NOTE
        Windows 版の読み戻しはアルファを捨てた 24bpp BGR を返す。
        呼び出し元がその前提なので、同じ形に揃える。
        ストライドは幅 * 4 と一致しない（CoreVideo が行ごとに詰め物をする）ので、
        必ずストライドを見て変換する。
    """
    width, height, stride, raw = frame
    image = Image.frombuffer("RGB", (width, height), raw, "raw", "BGRX", stride, 1)
    return width, height, image.tobytes("raw", "BGR")


class _StreamOutput(NSObject):
    """
    ScreenCaptureKit からフレームを受け取るデリゲート。
    """

    def initWithSink_(self, sink):
        self = objc.super(_StreamOutput, self).init()
        if self is None:
            return None
        self._sink = sink
        return self

    def stream_didOutputSampleBuffer_ofType_(self, stream, sample_buffer, output_type):
        if output_type != _OUTPUT_TYPE_SCREEN:
            return
        attachments = CMSampleBufferGetSampleAttachmentsArray(sample_buffer, False)
        if attachments:
            status = dict(attachments[0]).get("SCStreamUpdateFrameStatus", 0)
            # NOTE
            #   内容に変化がないフレームは complete 以外の状態で届く。
            #   ここで捨てることで「更新があった時だけ」という挙動になる。
            if status != _FRAME_STATUS_COMPLETE:
                return
        pixel_buffer = CMSampleBufferGetImageBuffer(sample_buffer)
        if pixel_buffer is None:
            return
        frame = _copy_pixels(pixel_buffer)
        if frame is None:
            return
        self._sink(
            frame,
            CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sample_buffer)),
        )


class Session:
    """
    キャプチャセッション

    このクラスのインスタンスが存命の間、
    バックグラウンドスレッド上でキャプチャが継続して実行されます。
    """

    def __init__(
        self,
        hwnd: int,
        duration_in_sec: float,
        max_width: Optional[int] = None,
        max_height: Optional[int] = None,
    ) -> None:
        """
        キャプチャセッションを開始する。

        Args:
            hwnd: キャプチャ対象ウィンドウの CGWindowID を int にしたもの。
            duration_in_sec: バッファ上に保持する秒数。
            max_width: キャプチャしたフレームの最大水平サイズ
            max_height: キャプチャしたフレームの最大垂直サイズ
        """
        self._hold_in_sec = duration_in_sec
        self._frames: deque[tuple[tuple[int, int, int, bytes], float]] = deque()
        self._guard = threading.Lock()
        self._stream = None
        self._does_track_stop = threading.Event()
        # NOTE
        #   SCStream はデリゲートを弱参照でしか持たない。
        #   Python 側で強参照を持たないと、
        #   エラーも出ないままコールバックが来なくなる。
        self._output = None

        # ウィンドウサーバへの接続を確立する
        # NOTE
        #   接続のないプロセスで SCContentFilter を作ると、
        #   例外ではなくアサート（CGS_REQUIRE_INIT）でプロセスごと落ちる。
        NSApplication.sharedApplication()

        content = _fetch_shareable_content()
        if content is None:
            raise RuntimeError("Failed to get shareable content")

        window = _find_window(content, hwnd)
        if window is None:
            raise RuntimeError(f"Window not found: {hwnd}")

        display = _find_display(content, window)
        if display is None:
            raise RuntimeError(f"Display not found for window: {hwnd}")

        self._window_id = hwnd
        self._max_width = max_width
        self._max_height = max_height
        self._display_frame = display.frame()

        # フィルタとキャプチャ範囲を決める
        # NOTE
        #   ウィンドウ単体のフィルタ（initWithDesktopIndependentWindow_）は、
        #   ウィンドウの一部しか描かれないフレームを返すことがある。
        #   ディスプレイのフィルタを対象ウィンドウだけに絞り、
        #   sourceRect でウィンドウの矩形を切り出す方法なら正しく取れる。
        content_filter = SCK.SCContentFilter.alloc().initWithDisplay_includingWindows_(
            display, [window]
        )
        self._point_pixel_scale = float(content_filter.pointPixelScale())
        self._source_rect = _resolve_source_rect(window.frame(), self._display_frame)
        self._capture_size = self._resolve_capture_size(self._source_rect)

        # ストリーム開始
        config = self._make_config(self._source_rect, self._capture_size)
        self._output = _StreamOutput.alloc().initWithSink_(self._push_frame)
        self._stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
            content_filter, config, None
        )
        ok, error = self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self._output, _OUTPUT_TYPE_SCREEN, None, None
        )
        if not ok:
            self._stream = None
            self._output = None
            raise RuntimeError(f"Failed to add stream output: {error}")

        result = _wait_async(self._stream.startCaptureWithCompletionHandler_)
        if result is None:
            self.Close()
            raise RuntimeError("Failed to start capture: timeout")
        error = result[0]
        if error is not None:
            self.Close()
            raise RuntimeError(f"Failed to start capture: {error}")

        # ウィンドウ追従スレッドを開始
        self._does_track_stop = threading.Event()
        self._track_thread = threading.Thread(
            target=self._track_window, name="aynime_capture_track", daemon=True
        )
        self._track_thread.start()

    def _resolve_capture_size(self, source_rect) -> tuple[int, int]:
        """
        ウィンドウの矩形（ポイント単位）から、キャプチャサイズ（ピクセル単位）を決める。
        """
        source_width = max(1, round(source_rect.size.width * self._point_pixel_scale))
        source_height = max(1, round(source_rect.size.height * self._point_pixel_scale))
        return _resolve_optimal_frame_size(
            source_width, source_height, self._max_width, self._max_height
        )

    def _make_config(self, source_rect, capture_size: tuple[int, int]):
        """
        ストリーム設定を作る。
        """
        config = SCK.SCStreamConfiguration.alloc().init()
        config.setSourceRect_(source_rect)
        config.setWidth_(capture_size[0])
        config.setHeight_(capture_size[1])
        # NOTE
        #   指定しないと 2 プレーンの YUV で届く。
        #   壊れずにそれらしく壊れた絵になるので、指定を忘れると気付きにくい。
        config.setPixelFormat_(Quartz.kCVPixelFormatType_32BGRA)
        config.setShowsCursor_(False)
        config.setMinimumFrameInterval_(CMTimeMake(1, _MAX_FRAME_RATE))
        config.setQueueDepth_(5)
        return config

    def _track_window(self) -> None:
        """
        キャプチャ対象ウィンドウの位置・サイズの変化に追従する。

        NOTE
            ウィンドウ矩形の取得には CGWindowListCopyWindowInfo を使う。
            対象ウィンドウ 1 件に絞れば実測 2.6 ms で済むので、
            この間隔で回しても負荷にならない。
        """
        while not self._does_track_stop.wait(_WINDOW_TRACK_INTERVAL_IN_SEC):
            if self._stream is None:
                return
            bounds = _get_window_bounds(self._window_id)
            if bounds is None:
                continue
            source_rect = _resolve_source_rect(bounds, self._display_frame)
            if _is_same_rect(source_rect, self._source_rect):
                continue
            capture_size = self._resolve_capture_size(source_rect)
            self._source_rect = source_rect
            self._capture_size = capture_size
            stream = self._stream
            if stream is None:
                return
            stream.updateConfiguration_completionHandler_(
                self._make_config(source_rect, capture_size), lambda error: None
            )

    def _push_frame(self, frame: tuple[int, int, int, bytes], time_in_sec: float) -> None:
        """
        フレームをバッファに追加する。

        NOTE
            保持秒数を超えた古いフレームを先頭から捨てる。
            「フレームなし」はできるだけ避けたいので、
            １フレームだけは削除せずに残す。
        """
        now_in_sec = time.monotonic()
        with self._guard:
            self._frames.append((frame, time_in_sec))
            while len(self._frames) > 1:
                if now_in_sec - self._frames[0][1] <= self._hold_in_sec:
                    break
                self._frames.popleft()

    def Close(self) -> None:
        """
        キャプチャセッションを停止する。
        """
        self._does_track_stop.set()
        if self._stream is not None:
            self._stream.stopCaptureWithCompletionHandler_(lambda error: None)
            self._stream = None
        self._output = None
        with self._guard:
            self._frames.clear()

    def GetFrameByTime(
        self, time_in_sec: float
    ) -> tuple[Optional[int], Optional[int], Optional[bytes]]:
        """
        指定した相対時刻に最も近いフレームを取得する。

        Args:
            time_in_sec: 最新フレームからの相対秒数 (例: 0.1)。

        Returns:
            (Width, Height, Frame Raw Buffer) のタプル。
            バックバッファに１枚もフレームがない場合 (None, None, None) を返す。
        """
        now_in_sec = time.monotonic()
        with self._guard:
            if not self._frames:
                return None, None, None
            frame, _ = min(
                self._frames,
                key=lambda entry: abs((now_in_sec - entry[1]) - time_in_sec),
            )
        return _to_bgr_bytes(frame)


class Snapshot:
    """
    キャプチャバッファスナップショット

    生成時点のバックバッファを固定したスナップショットを表します。
    バックグラウンドのキャプチャ処理の影響を受けません。
    """

    def __init__(
        self,
        session: Session,
        fps: Optional[float] = None,
        duration_in_sec: Optional[float] = None,
    ) -> None:
        raise NotImplementedError("macOS 版 Snapshot は未実装（M2 で実装）")

    def __enter__(self) -> "Snapshot":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    @property
    def size(self) -> int:
        raise NotImplementedError

    def GetFrame(self, frame_index: int) -> tuple[int, int, bytes]:
        raise NotImplementedError


def _fetch_shareable_content():
    """
    キャプチャ可能なウィンドウ・ディスプレイの一覧を取得する。
    """
    result = _wait_async(
        lambda handler: SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            True, False, handler
        )
    )
    if result is None:
        return None
    content, error = result
    if error is not None:
        return None
    return content


def _resolve_source_rect(window_frame, display_frame):
    """
    ウィンドウの矩形を、ディスプレイ原点からの相対矩形に直す。
    """
    return Quartz.CGRectMake(
        window_frame.origin.x - display_frame.origin.x,
        window_frame.origin.y - display_frame.origin.y,
        window_frame.size.width,
        window_frame.size.height,
    )


def _is_same_rect(lho, rho) -> bool:
    """
    2 つの矩形が同じか調べる。
    """
    return (
        lho.origin.x == rho.origin.x
        and lho.origin.y == rho.origin.y
        and lho.size.width == rho.size.width
        and lho.size.height == rho.size.height
    )


def _get_window_bounds(window_id: int):
    """
    CGWindowID からウィンドウの矩形を取得する。

    ウィンドウが存在しない場合は None を返す。
    """
    info_list = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionIncludingWindow, window_id
    )
    if not info_list:
        return None
    bounds = dict(info_list[0]).get("kCGWindowBounds")
    if bounds is None:
        return None
    return Quartz.CGRectMake(
        bounds["X"], bounds["Y"], bounds["Width"], bounds["Height"]
    )


def _find_window(content, window_id: int):
    """
    CGWindowID に対応する SCWindow を探す。
    """
    for window in content.windows():
        if int(window.windowID()) == window_id:
            return window
    return None


def _find_display(content, window):
    """
    ウィンドウの中心を含むディスプレイを探す。
    """
    frame = window.frame()
    center_x = frame.origin.x + frame.size.width / 2
    center_y = frame.origin.y + frame.size.height / 2
    for display in content.displays():
        bounds = display.frame()
        if (
            bounds.origin.x <= center_x < bounds.origin.x + bounds.size.width
            and bounds.origin.y <= center_y < bounds.origin.y + bounds.size.height
        ):
            return display
    return None
