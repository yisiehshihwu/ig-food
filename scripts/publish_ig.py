"""自動發 IG：每次執行最多發一篇，從 output/ 依序挑還沒發過的成品圖，貼文文字只放菜名。

設計成在 GitHub Actions 上每天跑（.github/workflows/ig-publish.yml），由這支腳本自己判斷
「是不是到了發文時間」：上次發文那天往後算 POST_INTERVAL_DAYS 天、當地 POST_TIME（預設
美東 19:00）。GitHub 的 cron 只能寫 UTC、也不會處理夏令時間，所以 workflow 每天跑兩次
（UTC 23:00 與 00:00，分別是夏令/冬令時間的美東 19:00），時間判斷一律在這裡用當地時區做。
只用標準函式庫，Actions 上不用裝套件。

發文用 Instagram 官方 Content Publishing API（graph.facebook.com）：
    1) POST /{ig-user-id}/media         image_url + caption -> 建立 container
    2) GET  /{container-id}?fields=status_code，等到 FINISHED
    3) POST /{ig-user-id}/media_publish creation_id -> 正式發出
API 會自己去抓 image_url，所以圖片必須放在公開網址（IMAGE_BASE_URL，例如 GitHub Pages）。

發文進度記在 output/publish_state.json：
    {"posted": {"三杯雞": {"posted_at": "...", "media_id": "...", "source": "api"}},
     "not_before": null, "priority": ["紅燒牛肉麵"]}
- source 為 "manual" 代表是在別處手動發過的，只是標記起來避免重複發。
- not_before（ISO 時間）可選：在這個時間之前不自動發。
- priority 可選：這些菜名插隊排在最前面（依列出的順序），其餘照 manifest 順序。

子命令：
    next                        印出下一篇會發哪張、什麼時候發（不需要 token）
    check                       不發文，只檢查 token、發文權限、圖片網址是否正常
    publish [--force]           到時間就發一篇；--force 忽略間隔立即發
    mark-posted <菜名>...       把手動發過/排過的菜名標記成已發
    unmark <菜名>...            取消標記
    set-not-before <ISO時間>    設定最早自動發文時間（當地時間；"none" 取消）
    set-priority <菜名>...      指定優先發的菜名與順序（"none" 清空）

環境變數（publish 需要）：
    IG_USER_ID          Instagram 商業帳號的 ID
    IG_ACCESS_TOKEN     有 instagram_content_publish 權限的 token（建議用不會過期的系統使用者 token）
    IMAGE_BASE_URL      output/ 圖片的公開網址前綴，例如 https://<帳號>.github.io/<repo>/output
    GRAPH_API_VERSION   預設 v26.0
    POST_INTERVAL_DAYS  預設 5
    POST_TIME           預設 19:00（當地時間）
    POST_TIMEZONE       預設 America/New_York（使用者在美東，會自動處理夏令/冬令時間）
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
STATE_PATH = OUTPUT_DIR / "publish_state.json"

LOCAL_TZ = ZoneInfo(os.environ.get("POST_TIMEZONE", "America/New_York"))
POST_TIME = dtime.fromisoformat(os.environ.get("POST_TIME", "19:00"))


def _now() -> datetime:
    return datetime.now(LOCAL_TZ)


def _load_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _load_state() -> dict:
    state = _load_json(STATE_PATH, {})
    state.setdefault("posted", {})
    state.setdefault("not_before", None)
    state.setdefault("priority", [])
    return state


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _queue() -> list[str]:
    """發文順序 = manifest 裡的定案順序（新照片 finalize 後自然排在最後），去重、只留檔案存在的。"""
    seen, labels = set(), []
    for label in _load_json(MANIFEST_PATH, {}).values():
        if label not in seen and (OUTPUT_DIR / f"{label}.jpg").exists():
            seen.add(label)
            labels.append(label)
    return labels


def _caption(label: str) -> str:
    # 撞名時檔名會是「紅燒排骨2」，貼文文字不要帶那個數字
    return re.sub(r"\d+$", "", label).strip() or label


def _ordered_queue(state: dict) -> list[str]:
    queue = _queue()
    first = [l for l in state["priority"] if l in queue]
    return first + [l for l in queue if l not in first]


def _next_label(state: dict) -> str | None:
    return next((l for l in _ordered_queue(state) if l not in state["posted"]), None)


def _next_due(state: dict) -> datetime | None:
    """下次發文時間 = 上次 API 發文那天 + 間隔天數的當地 POST_TIME；用「日期」算而不是精確
    相隔幾小時，這樣 GitHub cron 延遲幾十分鐘也不會讓下一篇整整晚一天。"""
    interval = int(os.environ.get("POST_INTERVAL_DAYS", "5"))
    api_times = [
        datetime.fromisoformat(v["posted_at"]).astimezone(LOCAL_TZ)
        for v in state["posted"].values()
        if v.get("source") == "api" and v.get("posted_at")
    ]
    candidates = []
    if api_times:
        day = max(api_times).date() + timedelta(days=interval)
        candidates.append(datetime.combine(day, POST_TIME, LOCAL_TZ))
    if state["not_before"]:
        candidates.append(datetime.fromisoformat(state["not_before"]).astimezone(LOCAL_TZ))
    return max(candidates) if candidates else None


def _graph(method: str, path: str, params: dict) -> dict:
    version = os.environ.get("GRAPH_API_VERSION", "v26.0")
    url = f"https://graph.facebook.com/{version}/{path}"
    data = urllib.parse.urlencode(params).encode()
    if method == "GET":
        req = urllib.request.Request(f"{url}?{data.decode()}")
    else:
        req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Graph API 錯誤 {e.code}（{path}）：{e.read().decode(errors='replace')}")


def _check_image_url(url: str) -> None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as resp:
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.URLError as e:
        raise SystemExit(f"圖片網址打不開，IG 也會抓不到：{url}（{e}）")
    if "image/jpeg" not in ctype:
        raise SystemExit(f"圖片網址的 Content-Type 是 {ctype!r} 不是 image/jpeg，IG 可能會拒收：{url}")


def _secret(name: str) -> str:
    # 貼進 GitHub Secrets 時很容易多帶到結尾換行（第一次上線就是 IG_USER_ID 結尾多了
    # 換行而失敗），一律去掉前後空白
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"GitHub Secrets 裡沒有設定 {name}")
    return value


def _image_url(label: str) -> str:
    """成品圖在 GitHub Pages 上的公開網址。IMAGE_BASE_URL 沒設定就從 GITHUB_REPOSITORY
    （Actions 自帶，例如 yisiehshihwu/ig-food）推算；有設定但漏了結尾的 /output 也自動補上——
    第一次上線就因為少了 /output 而 404。"""
    base = os.environ.get("IMAGE_BASE_URL", "").strip().rstrip("/")
    if not base:
        owner, repo = os.environ["GITHUB_REPOSITORY"].split("/")
        base = f"https://{owner}.github.io/{repo}"
    if not base.endswith("/output"):
        base += "/output"
    return f"{base}/{urllib.parse.quote(label)}.jpg"


def _publish_one(label: str) -> str:
    user_id = _secret("IG_USER_ID")
    token = _secret("IG_ACCESS_TOKEN")
    image_url = _image_url(label)
    _check_image_url(image_url)

    container = _graph("POST", f"{user_id}/media", {
        "image_url": image_url, "caption": _caption(label), "access_token": token,
    })["id"]
    for _ in range(30):
        status = _graph("GET", container, {"fields": "status_code", "access_token": token}).get("status_code")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise SystemExit(f"IG 處理圖片失敗（status={status}）：{label}")
        time.sleep(5)
    else:
        raise SystemExit(f"等 IG 處理圖片逾時：{label}")
    return _graph("POST", f"{user_id}/media_publish", {"creation_id": container, "access_token": token})["id"]


def cmd_next(_args) -> None:
    state = _load_state()
    label = _next_label(state)
    remaining = sum(1 for l in _queue() if l not in state["posted"])
    if not label:
        print("佇列是空的：output/ 的成品都發過了。")
        return
    due = _next_due(state)
    when = "下一次排程執行時就會發" if not due or due <= _now() else f"{due:%Y-%m-%d %H:%M} 之後的第一次排程執行"
    print(f"下一篇：{label}（貼文文字：{_caption(label)}）\n時間：{when}\n佇列剩 {remaining} 張")


def cmd_publish(args) -> None:
    state = _load_state()
    label = _next_label(state)
    if not label:
        print("佇列是空的，沒有可以發的。")
        return
    due = _next_due(state)
    if not args.force and due and _now() < due:
        print(f"還沒到發文時間（{due:%Y-%m-%d %H:%M} 之後），這次不發。")
        return
    media_id = _publish_one(label)
    state["posted"][label] = {"posted_at": _now().isoformat(timespec="seconds"), "media_id": media_id, "source": "api"}
    _save_state(state)
    print(f"已發布：{label}（media_id={media_id}）")


def cmd_check(_args) -> None:
    """不發文，只確認 token、IG 帳號、發文權限、下一張圖的網址都正常。"""
    user_id = _secret("IG_USER_ID")
    token = _secret("IG_ACCESS_TOKEN")
    account = _graph("GET", user_id, {"fields": "username", "access_token": token})
    print(f"token 有效，IG 帳號：@{account.get('username')}")
    # 這個端點需要 instagram_content_publish 權限，順便確認發文權限有給到
    limit = _graph("GET", f"{user_id}/content_publishing_limit", {"fields": "quota_usage,config", "access_token": token})
    print(f"發文權限正常，過去 24 小時已用額度：{limit.get('data', [{}])[0].get('quota_usage')}")
    state = _load_state()
    label = _next_label(state)
    if label:
        url = _image_url(label)
        _check_image_url(url)
        print(f"下一張圖片網址正常：{label} -> {url}")
    cmd_next(_args)
    print("檢查完成，全部正常（這次沒有發文）。")


def cmd_mark_posted(args) -> None:
    state = _load_state()
    known = set(_queue())
    for label in args.labels:
        if label not in known:
            raise SystemExit(f"output/ 找不到這個菜名：{label}")
        state["posted"].setdefault(label, {"source": "manual"})
    _save_state(state)
    print(f"已標記 {len(args.labels)} 張為已發。")


def cmd_unmark(args) -> None:
    state = _load_state()
    for label in args.labels:
        state["posted"].pop(label, None)
    _save_state(state)


def cmd_set_not_before(args) -> None:
    state = _load_state()
    if args.when.lower() == "none":
        state["not_before"] = None
    else:
        when = datetime.fromisoformat(args.when)
        state["not_before"] = (when if when.tzinfo else when.replace(tzinfo=LOCAL_TZ)).isoformat(timespec="minutes")
    _save_state(state)
    print(f"not_before = {state['not_before']}")


def cmd_set_priority(args) -> None:
    state = _load_state()
    if args.labels == ["none"]:
        state["priority"] = []
    else:
        known = set(_queue())
        missing = [l for l in args.labels if l not in known]
        if missing:
            raise SystemExit(f"output/ 找不到這些菜名：{missing}")
        state["priority"] = args.labels
    _save_state(state)
    print(f"priority = {state['priority']}")


def cmd_schedule(args) -> None:
    """推算接下來每一篇的預計發文日（假設每次都準時發出）。"""
    state = _load_state()
    pending = [l for l in _ordered_queue(state) if l not in state["posted"]]
    due = _next_due(state) or datetime.combine(_now().date(), POST_TIME, LOCAL_TZ)
    if due < _now():
        due = datetime.combine(_now().date(), POST_TIME, LOCAL_TZ)
        if due < _now():
            due += timedelta(days=1)
    interval = timedelta(days=int(os.environ.get("POST_INTERVAL_DAYS", "5")))
    for i, label in enumerate(pending[: args.limit]):
        print(f"{(due + interval * i):%Y-%m-%d %a %H:%M %Z}  {label}")
    if len(pending) > args.limit:
        print(f"...（共 {len(pending)} 張）")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("next").set_defaults(func=cmd_next)
    sub.add_parser("check").set_defaults(func=cmd_check)
    p = sub.add_parser("publish")
    p.add_argument("--force", action="store_true", help="忽略發文間隔，立即發下一篇")
    p.set_defaults(func=cmd_publish)
    p = sub.add_parser("mark-posted")
    p.add_argument("labels", nargs="+")
    p.set_defaults(func=cmd_mark_posted)
    p = sub.add_parser("unmark")
    p.add_argument("labels", nargs="+")
    p.set_defaults(func=cmd_unmark)
    p = sub.add_parser("set-not-before")
    p.add_argument("when", help='例如 "2026-10-25T19:00"（POST_TIMEZONE 的當地時間），或 none')
    p.set_defaults(func=cmd_set_not_before)
    p = sub.add_parser("set-priority")
    p.add_argument("labels", nargs="+")
    p.set_defaults(func=cmd_set_priority)
    p = sub.add_parser("schedule")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_schedule)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
