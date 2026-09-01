"""
Snapshot の自己検査。

実機のキャプチャを必要としない範囲を確認する。
Windows 版（core.cpp の ayc::Snapshot と frame_buffer.cpp の FreezedFrameBuffer）
から導出した仕様どおりに動くかを見る。

    ../.venv/bin/python test/test_snapshot.py
"""

# std
from collections import deque
import threading
import time

# self
from aynime_capture.session import Snapshot, _resolve_index_map


def _make_frame(value: int) -> tuple[int, int, int, bytes]:
    """
    2x2 の 32BGRA フレームを作る。全画素を value で塗る。
    """
    WIDTH, HEIGHT, STRIDE = 2, 2, 8
    return (WIDTH, HEIGHT, STRIDE, bytes([value]) * (STRIDE * HEIGHT))


class _StubSession:
    """
    Session のうち Snapshot が触る部分だけを持つスタブ。
    """

    def __init__(self, frames: list[tuple[tuple, float]], hold_in_sec: float):
        self._frames = deque(frames)
        self._guard = threading.Lock()
        self._hold_in_sec = hold_in_sec
        self._stream = object()  # NOTE 停止していないことを示す


def _make_session(ages_in_sec: list[float], hold_in_sec: float = 5.0) -> _StubSession:
    """
    指定した経過秒数のフレームを持つスタブセッションを作る。
    フレームの塗り値はリストの並び順（0, 1, 2, ...）。
    """
    now_in_sec = time.monotonic()
    return _StubSession(
        [
            (_make_frame(index), now_in_sec - age_in_sec)
            for index, age_in_sec in enumerate(ages_in_sec)
        ],
        hold_in_sec,
    )


def test_index_map_empty():
    """フレームが無ければ空の写像"""
    assert _resolve_index_map([], None, None) == []
    assert _resolve_index_map([], 10.0, 2.0) == []


def test_index_map_identity():
    """fps 指定が無ければ恒等写像"""
    assert _resolve_index_map([0.3, 0.2, 0.1, 0.0], None, None) == [0, 1, 2, 3]
    assert _resolve_index_map([0.3, 0.2, 0.1, 0.0], None, 0.2) == [0, 1, 2, 3]


def test_index_map_thinning():
    """fps 指定があればその fps でキャプチャしたかのように間引かれる"""
    # 0.1 sec 刻みで 2.0 sec 分（21 枚）を 10 fps で 2.0 sec 分に間引く
    ages_in_sec = [round(2.0 - 0.1 * i, 3) for i in range(21)]
    index_map = _resolve_index_map(ages_in_sec, 10.0, 2.0)

    # 枚数は duration * fps
    assert len(index_map) == 20

    # 古い順に並んでいる（インデックスが単調非減少）
    assert all(
        index_map[i] <= index_map[i + 1] for i in range(len(index_map) - 1)
    ), index_map

    # 先頭が最も古く、末尾が最も新しい
    assert ages_in_sec[index_map[0]] > ages_in_sec[index_map[-1]]

    # 末尾は最新フレーム（経過秒数 0）を指す
    assert ages_in_sec[index_map[-1]] == 0.0


def test_index_map_duplicates_allowed():
    """実キャプチャより高い fps を指定すると同じフレームが複数回選ばれる"""
    index_map = _resolve_index_map([0.2, 0.1, 0.0], 100.0, 0.2)
    assert len(index_map) == 20
    assert len(set(index_map)) < len(index_map)


def test_index_map_rounding_matches_cpp():
    """枚数の丸めは std::round と同じ（0.5 は切り上げ）"""
    # 0.5 sec * 5 fps = 2.5 -> 3 枚
    # NOTE Python 組み込みの round は偶数丸めなので 2 枚になってしまう
    assert len(_resolve_index_map([0.5, 0.25, 0.0], 5.0, 0.5)) == 3


def test_snapshot_orders_oldest_first():
    """スナップショットは古い順に並ぶ"""
    # 追加順をわざとバラバラにする
    session = _make_session([0.1, 0.3, 0.2])
    with Snapshot(session, None, None) as snapshot:
        assert snapshot.size == 3
        # 塗り値 1 が最も古い（0.3 sec 前）、0 が最も新しい（0.1 sec 前）
        assert snapshot.GetFrame(0)[2][0] == 1
        assert snapshot.GetFrame(1)[2][0] == 2
        assert snapshot.GetFrame(2)[2][0] == 0


def test_snapshot_returns_packed_bgr():
    """返るのは隙間なく詰めた 24bpp BGR"""
    session = _make_session([0.1])
    with Snapshot(session, None, None) as snapshot:
        width, height, frame_bytes = snapshot.GetFrame(0)
        assert (width, height) == (2, 2)
        assert len(frame_bytes) == width * height * 3


def test_snapshot_clips_by_duration():
    """指定区間の外のフレームは落ちる"""
    session = _make_session([0.05, 0.10, 0.50, 1.00])
    with Snapshot(session, None, 0.2) as snapshot:
        assert snapshot.size == 2


def test_snapshot_clamps_to_hold_duration():
    """保持秒数を超える指定は保持秒数に丸められる"""
    session = _make_session([0.1, 1.0, 3.0], hold_in_sec=2.0)
    with Snapshot(session, None, 10.0) as snapshot:
        assert snapshot.size == 2


def test_snapshot_keeps_at_least_one_frame():
    """区間内に１枚も無くても、最低１枚は残す"""
    session = _make_session([1.0, 2.0])
    with Snapshot(session, None, 0.01) as snapshot:
        assert snapshot.size == 1
        # 残るのはバッファの末尾（＝最後に追加されたもの）
        assert snapshot.GetFrame(0)[2][0] == 1


def test_snapshot_empty_buffer():
    """バッファが空ならフレーム数 0。例外は投げない"""
    session = _StubSession([], 5.0)
    with Snapshot(session, 10.0, 2.0) as snapshot:
        assert snapshot.size == 0


def test_snapshot_exit_releases_frames():
    """コンテキストを抜けたらフレームを手放す"""
    session = _make_session([0.1, 0.2])
    snapshot = Snapshot(session, None, None)
    assert snapshot.size == 2
    snapshot.__exit__(None, None, None)
    assert snapshot.size == 0


def test_snapshot_out_of_bounds():
    """範囲外のインデックスはエラー"""
    session = _make_session([0.1])
    with Snapshot(session, None, None) as snapshot:
        for bad_index in (1, -1):
            try:
                snapshot.GetFrame(bad_index)
            except IndexError:
                pass
            else:
                raise AssertionError(f"IndexError not raised ({bad_index})")


def test_snapshot_stopped_session():
    """停止済みセッションからはスナップショットを取れない"""
    session = _make_session([0.1])
    session._stream = None
    try:
        Snapshot(session, None, None)
    except RuntimeError:
        pass
    else:
        raise AssertionError("RuntimeError not raised")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
