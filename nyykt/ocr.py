"""Windows 自带 OCR（Windows.Media.Ocr），支持简体中文。

不需要外部服务，也不需要额外模型。
注意：Playwright sync API 运行时会占住主线程的事件循环，
所以这里每次都在独立线程里新建 loop。
"""

from __future__ import annotations

import asyncio
import os
import threading


def available() -> bool:
    try:
        from winsdk.windows.globalization import Language
        from winsdk.windows.media.ocr import OcrEngine

        return bool(OcrEngine.is_language_supported(Language("zh-Hans-CN")))
    except Exception:
        return False


async def _run(path: str) -> str:
    from winsdk.windows.globalization import Language
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage import FileAccessMode, StorageFile

    eng = OcrEngine.try_create_from_language(Language("zh-Hans-CN"))
    if eng is None:
        return ""
    f = await StorageFile.get_file_from_path_async(os.path.abspath(path))
    stream = await f.open_async(FileAccessMode.READ)
    dec = await BitmapDecoder.create_async(stream)
    bmp = await dec.get_software_bitmap_async()
    res = await eng.recognize_async(bmp)
    return "\n".join("".join(w.text for w in ln.words) for ln in res.lines)


def ocr_png(path: str, timeout: float = 60.0) -> str:
    """同步入口：独立线程 + 独立事件循环，避开 Playwright 的主循环。"""
    box: dict = {"text": "", "err": None}

    def work() -> None:
        loop = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            box["text"] = loop.run_until_complete(_run(path))
        except Exception as e:  # noqa: BLE001
            box["err"] = e
        finally:
            try:
                if loop is not None:
                    loop.close()
            except Exception:
                pass
            try:
                asyncio.set_event_loop(None)
            except Exception:
                pass

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"OCR 超时（{timeout}s）")
    if box["err"] is not None:
        raise box["err"]
    return box["text"]
