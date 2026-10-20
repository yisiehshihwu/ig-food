#!/usr/bin/env bash
# 觸發 GitHub Actions 的 IG auto publish（強制立刻發下一篇）。
# 用 git 存的 GitHub 登入憑證呼叫 API，token 不會印出來或寫進檔案。
# 用法：bash scripts/trigger_publish.sh            # 立刻發下一篇
#       bash scripts/trigger_publish.sh --check    # 只檢查設定，不發文
set -euo pipefail

REPO="yisiehshihwu/ig-food"
if [ "${1:-}" = "--check" ]; then
  INPUTS='{"check_only":true,"force":false}'
else
  INPUTS='{"check_only":false,"force":true}'
fi

TOKEN=$(printf "protocol=https\nhost=github.com\n\n" | git credential fill 2>/dev/null | sed -n 's/^password=//p')
if [ -z "$TOKEN" ]; then
  echo "找不到 GitHub 憑證（git credential fill 沒回傳 token）" >&2
  exit 1
fi

code=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$REPO/actions/workflows/ig-publish.yml/dispatches" \
  -d "{\"ref\":\"main\",\"inputs\":$INPUTS}")

if [ "$code" = "204" ]; then
  echo "已觸發 workflow：https://github.com/$REPO/actions/workflows/ig-publish.yml"
else
  echo "觸發失敗，HTTP $code" >&2
  exit 1
fi
