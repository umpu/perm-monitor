#!/bin/bash
# perm-monitor-watch — страховка расписания GitHub Actions.
# LaunchAgent запускает раз в 5 минут: если у workflow не было ни одного прогона
# дольше MAX_AGE секунд, запускаем его вручную через gh workflow run.
# Когда штатное расписание GitHub работает, скрипт ничего не делает.
set -u
REPO="umpu/perm-monitor"
WF="monitor.yml"
MAX_AGE=420                      # 7 минут: штатное расписание — каждые 5
LOG="$HOME/Library/Logs/perm-monitor-watch.log"
LOCK="/tmp/perm-monitor-watch.lock"
GH="/Users/macbookpro/bin/gh"

mkdir "$LOCK" 2>/dev/null || exit 0          # уже идёт другой экземпляр
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG"; }

last=$("$GH" api "repos/$REPO/actions/runs?per_page=1" -q '.workflow_runs[0].created_at' 2>/dev/null)
if [ -z "$last" ] || [ "$last" = "null" ]; then
  log "не прочитал последний прогон (gh или сеть)"
  exit 1
fi
# Время в API — UTC; без TZ=UTC date -j разбирает строку как местное и врёт на смещение зоны.
ts=$(TZ=UTC date -j -f "%Y-%m-%dT%H:%M:%SZ" "$last" +%s 2>/dev/null)
if [ -z "$ts" ]; then
  log "не разобрал время: $last"
  exit 1
fi
age=$(( $(date +%s) - ts ))
if [ "$age" -gt "$MAX_AGE" ]; then
  if "$GH" workflow run "$WF" --repo "$REPO" >>"$LOG" 2>&1; then
    log "прогонов не было ${age}s — запустил вручную"
  else
    log "ручной запуск не удался (простой ${age}s)"
  fi
fi
