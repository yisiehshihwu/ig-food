"""把手機上傳到雲端硬碟（Google Drive）資料夾的新照片，複製進專案的 input/。

使用者原本是 手機 → Messenger → 下載到電腦 → 手動放進 input/，又慢又會被 Messenger
壓縮成 2048px。改成手機直接分享到 Google Drive 的資料夾，電腦裝 Google Drive 桌面版後
那個資料夾會出現在 G: 槽，這裡負責把裡面「還沒進過 input/」的照片複製過來，之後的
list-new / preview / finalize 流程完全不用改。

雲端資料夾的路徑寫在專案根目錄的 input_import_dir.txt（一行路徑；不進版控，因為是
這台電腦專屬的設定）。沒有這個檔案就什麼都不做。

只用標準函式庫，給 process_photo.py 與 auto_preview.py（hook，要輕量）共用。
"""

import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = PROJECT_ROOT / "input"
CONFIG_PATH = PROJECT_ROOT / "input_import_dir.txt"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}


def import_dir() -> Path | None:
    if not CONFIG_PATH.exists():
        return None
    text = CONFIG_PATH.read_text(encoding="utf-8-sig").strip()
    return Path(text) if text else None


def import_new_photos() -> list[Path]:
    """複製雲端資料夾裡 input/ 還沒有的照片（用檔名判斷），回傳複製進來的新路徑。
    不會刪除或修改雲端資料夾裡的檔案。雲端資料夾不存在（例如 Google Drive 沒開）就跳過。"""
    src = import_dir()
    if src is None or not src.is_dir():
        return []
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    copied = []
    for f in sorted(src.iterdir()):
        if not f.is_file() or f.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        dest = INPUT_DIR / f.name
        if dest.exists():
            continue
        shutil.copy2(f, dest)  # Google Drive 串流模式下，讀檔時才會真的下載
        copied.append(dest)
    return copied
