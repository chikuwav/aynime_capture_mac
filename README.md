# aynime_capture (macOS)

[Nu-Pan/aynime_capture](https://github.com/Nu-Pan/aynime_capture) の macOS 版。
Windows 版が Windows.Graphics.Capture + Direct3D 11 の C++ 実装で提供している API を、
ScreenCaptureKit を使った純 Python 実装で提供する。

import 名は Windows 版と同じ `aynime_capture` なので、呼び出し側は OS を意識しない。

## 由来とライセンス

MIT ライセンス。API 仕様・docstring は Nu-Pan/aynime_capture（MIT, Copyright (c) 2025 NU-Pan）に由来する。
著作権表示は LICENSE に保持している。原作者の許可を得て公開している。

**原作リポジトリへは変更を送らない。** このリポジトリのコードは AI が生成したもので、
原作の開発規約が AI エージェントによるファイル編集を禁じているため。

## 必須環境

- macOS 12.3 以上（ScreenCaptureKit）
- Python 3.11 以上
- 画面収録の許可（システム設定 > プライバシーとセキュリティ > 画面収録）

依存は pyobjc の 3 パッケージ（ScreenCaptureKit / Quartz / Cocoa）だけで、
インストール時に自動で入る。

**画面収録の許可は、プロセスを起動したアプリに紐づく。**
ターミナルから動かすならターミナルに許可を与えることになる。
また、macOS は許可の状態を起動時にしか読み直さないので、
**許可を与えたあとは起動元のアプリごと再起動する**こと。そうしないとウィンドウが 1 件も列挙されない。

## インストール

**[えぃにめ一閃流 macOS 版](https://github.com/chikuwav/aynime_issen_style_mac) を使うだけなら、
このリポジトリを手で入れる必要はない。** 本体の依存として自動で入る。

単体で使う場合:

```
pip install "git+https://github.com/chikuwav/aynime_capture_mac"
```

このリポジトリ自体をいじる場合は、clone して editable install:

```
pip install -e .
```

## テスト

実機のキャプチャを必要としない範囲の自己検査。

```
python test/test_snapshot.py
```

## 実装状況

| API | 状態 |
|---|---|
| `Session`（キャプチャ開始・リングバッファ・静止画取得） | ✅ 実装済み |
| `Snapshot`（動画キャプチャ用のバッファ固定） | ✅ 実装済み |
| `set_log_handle` | ✅ no-op として実装済み |

## API

- `Session(hwnd, duration_in_sec, max_width, max_height)` — `hwnd` は CGWindowID
- `Session.GetFrameByTime(time_in_sec) -> (width, height, bytes) | (None, None, None)`
- `Session.Close()`
- `Snapshot(session, fps, duration_in_sec)` / `.size` / `.GetFrame(index)`
- `set_log_handle(handle)` — macOS では no-op

フレームのバイト列は、隙間なく詰めた 24bpp BGR（Windows 版と同じ）。
