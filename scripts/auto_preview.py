"""Claude Code hook：自動檢查 input/ 有沒有新照片，有的話先用 auto-detect 的參數
產生預覽圖到 .cache/previews/，等使用者在對話裡確認。不會碰 output/，也不會 finalize。

由 .claude/settings.json 的 SessionStart / UserPromptSubmit hook 呼叫，stdin 是 hook
的 JSON payload（只用到 hook_event_name）。
- 每張預覽旁邊會存一份同名 .json，記錄產生預覽用的參數，finalize 時直接沿用。
- 已經記錄在 manifest.json（已定案）的照片，殘留的預覽檔會被清掉。
- 一次最多產生 BATCH_SIZE 張，跟 ig-photo-batch skill 的分批習慣一致。
- 每 CHECK_INTERVAL 才真正檢查一次（使用者要求每週一次）：hook 每次都會被呼叫，
  但距離上次檢查不滿一週就直接結束，所以實際上是「滿一週後的第一次開對話/送訊息」才檢查。
  檢查時回報所有等待確認的預覽，包含上次產生但還沒定案的。
"""

import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = PROJECT_ROOT / "input"
PREVIEW_DIR = PROJECT_ROOT / ".cache" / "previews"
MANIFEST_PATH = PROJECT_ROOT / "output" / "manifest.json"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
BATCH_SIZE = 5
CHECK_INTERVAL = 7 * 24 * 3600  # 秒；每週檢查一次
LAST_CHECK_PATH = PROJECT_ROOT / ".cache" / "auto_preview_last_check"


def _due_for_check() -> bool:
    try:
        last = float(LAST_CHECK_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return time.time() - last >= CHECK_INTERVAL


def _mark_checked() -> None:
    LAST_CHECK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_CHECK_PATH.write_text(str(time.time()), encoding="utf-8")


def _read_event() -> str:
    try:
        return json.loads(sys.stdin.read() or "{}").get("hook_event_name", "")
    except (json.JSONDecodeError, OSError):
        return ""


def _pending_inputs(manifest: dict) -> list[Path]:
    return [
        p
        for p in sorted(INPUT_DIR.glob("*"))
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS and p.stem not in manifest
    ]


def _clear_finalized_previews(manifest: dict) -> None:
    if not PREVIEW_DIR.exists():
        return
    for f in PREVIEW_DIR.iterdir():
        if f.is_file() and f.stem in manifest:
            f.unlink()


def _generate(paths: list[Path]) -> list[dict]:
    # 有新照片才載入 OpenCV/Pillow，沒事的時候 hook 幾乎不花時間
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import process_photo as pp

    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for path in paths:
        detected = pp.auto_detect_params(path)
        params = {"offset_x": 0.0, "offset_y": 0.0, "zoom": 1.0, "rotate": 0.0}
        if detected:
            params.update({k: detected[k] for k in params})
        image = pp.process(path, params["offset_x"], params["offset_y"], params["zoom"], rotate=params["rotate"])
        image.save(PREVIEW_DIR / f"{path.stem}.jpg", quality=90)
        record = {"input": str(path), "detected": bool(detected), **params}
        (PREVIEW_DIR / f"{path.stem}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        results.append(record)
    return results


def main() -> None:
    event = _read_event()
    if not _due_for_check():
        return
    _mark_checked()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.exists() else {}
    _clear_finalized_previews(manifest)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cloud_import import import_new_photos

    import_new_photos()  # 手機傳到 Google Drive 的新照片先拉進 input/
    pending = _pending_inputs(manifest)
    if not pending:
        return

    missing = [p for p in pending if not (PREVIEW_DIR / f"{p.stem}.json").exists()]
    already = len(pending) - len(missing)
    to_make = missing[: max(0, BATCH_SIZE - already)]
    if to_make:
        _generate(to_make)

    report = [
        json.loads((PREVIEW_DIR / f"{p.stem}.json").read_text(encoding="utf-8"))
        for p in pending
        if (PREVIEW_DIR / f"{p.stem}.json").exists()
    ]
    if not report:
        return

    lines = [
        f"[auto_preview] input/ 有 {len(pending)} 張新照片尚未定案，已自動產生 {len(report)} 張預覽在 .cache/previews/：",
    ]
    for r in report:
        stem = Path(r["input"]).stem
        note = "" if r["detected"] else "（auto-detect 失敗，用預設參數）"
        lines.append(
            f"- {r['input']} -> .cache/previews/{stem}.jpg  "
            f"--offset-x {r['offset_x']} --offset-y {r['offset_y']} --zoom {r['zoom']} --rotate {r['rotate']}{note}"
        )
    lines.append(
        "請照 ig-photo-batch skill：把預覽圖顯示給使用者確認（含檢查 4:3 比例），使用者同意後才用同樣參數 finalize；"
        "回答使用者原本的問題時順帶提一下即可。"
    )
    output = {
        "systemMessage": f"每週檢查：有 {len(report)} 張新照片，已先產生預覽圖等你確認",
        "hookSpecificOutput": {"hookEventName": event or "SessionStart", "additionalContext": "\n".join(lines)},
    }
    print(json.dumps(output))  # ensure_ascii 預設 True，避免 Windows 主控台編碼問題


if __name__ == "__main__":
    main()
