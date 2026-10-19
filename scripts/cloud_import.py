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
# 匯入過的檔名紀錄（一行一個）。有了它，使用者決定不用某張照片時，只要刪掉 input/ 裡那份
# 就好，不會因為雲端資料夾裡還有原檔又被複製回來；也不用去動使用者雲端硬碟上的檔案。
IMPORTED_LOG = PROJECT_ROOT / "input_imported.txt"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}


def _imported_names() -> set[str]:
    if not IMPORTED_LOG.exists():
        return set()
    return {line.strip() for line in IMPORTED_LOG.read_text(encoding="utf-8").splitlines() if line.strip()}


def import_dir() -> Path | None:
    if not CONFIG_PATH.exists():
        return None
    text = CONFIG_PATH.read_text(encoding="utf-8-sig").strip()
    return Path(text) if text else None


def import_new_photos() -> list[Path]:
    """複製雲端資料夾裡還沒匯入過的照片（用檔名判斷），回傳複製進來的新路徑。
    不會刪除或修改雲端資料夾裡的檔案。雲端資料夾不存在（例如 Google Drive 沒開）就跳過。"""
    src = import_dir()
    if src is None or not src.is_dir():
        return []
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    imported = _imported_names()
    copied = []
    for f in sorted(src.iterdir()):
        if not f.is_file() or f.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        dest = INPUT_DIR / f.name
        if f.name in imported or dest.exists():
            continue
        shutil.copy2(f, dest)  # Google Drive 串流模式下，讀檔時才會真的下載
        copied.append(dest)
    if copied:
        with IMPORTED_LOG.open("a", encoding="utf-8") as log:
            log.writelines(f"{p.name}\n" for p in copied)
    return copied
