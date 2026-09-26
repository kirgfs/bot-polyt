#!/usr/bin/env bash
# Переносит отчёты с VPS в отдельный приватный git-репозиторий и удаляет локальные копии.
# Что уходит: все data/reports/**/*.md (ежедневные m1_<дата>.md, контроль OddsPapi, разовые
# проверки) и текущий data/state/recorder_status.json. Секретов там нет: ключи в Parquet,
# логи и отчёты не пишутся (CLAUDE.md, правило 2).
# Настройка (один раз) — docs/runbook_m1.md §6. Запуск — cron после `polybot daily`.
set -euo pipefail

POLYBOT_DIR="${POLYBOT_DIR:-/root/polybot}"
REPORTS_REPO="${REPORTS_REPO:-/root/polybot-reports}"
SRC="$POLYBOT_DIR/data/reports"
STATUS="$POLYBOT_DIR/data/state/recorder_status.json"
stamp() { date -u +%FT%TZ; }

cd "$REPORTS_REPO"
git pull --quiet --ff-only

files=()
if [[ -d "$SRC" ]]; then
  mapfile -t files < <(cd "$SRC" && find . -type f -name '*.md' | sort)
fi
for f in "${files[@]}"; do
  mkdir -p "$(dirname "$f")"
  cp "$SRC/$f" "$f"
done
if [[ -f "$STATUS" ]]; then
  mkdir -p status
  cp "$STATUS" status/recorder_status.json
fi

git add -A .
if git diff --cached --quiet; then
  echo "$(stamp) nothing to publish"
  exit 0
fi
git commit --quiet -m "VPS: отчёты $(date -u +%F)"
git push --quiet
# Удаляем только после успешного push: при сбое отчёты уйдут в следующий раз.
for f in "${files[@]}"; do
  rm -f "$SRC/$f"
done
echo "$(stamp) published ${#files[@]} report(s)"
