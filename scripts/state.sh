#!/usr/bin/env bash
# pull: ينزّل الحالة (sqlite + JSON) من فرع data والنماذج من Release
# push: يرفع الحالة إلى فرع data كـ commit واحد (force) حتى لا يكبر المستودع
set -euo pipefail
REMOTE="${STATE_REMOTE:-https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git}"

case "${1:-}" in
pull)
  mkdir -p state/models
  if git ls-remote --exit-code --heads "$REMOTE" data >/dev/null 2>&1; then
    rm -rf /tmp/statesrc
    git clone --quiet --depth 1 --branch data "$REMOTE" /tmp/statesrc
    cp -a /tmp/statesrc/. state/ && rm -rf state/.git
  elif [ -f seed/iq_ai_data.sqlite3 ]; then
    echo "أول تشغيل: استخدام بيانات seed"
    cp seed/iq_ai_data.sqlite3 state/
  fi
  mkdir -p state/models
  if gh release download models-latest -p models.tar.gz -D /tmp --clobber 2>/dev/null; then
    tar xzf /tmp/models.tar.gz -C state
  else
    echo "لا توجد نماذج بعد — شغّل workflow التدريب"
  fi
  ;;
push)
  cd state
  printf 'models/\n' > .gitignore
  rm -rf .git
  git init -q -b data
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  git add -A
  git commit -q -m "state $(date -u +%FT%TZ)"
  git push -q -f "$REMOTE" data
  ;;
upload-models)
  tar czf /tmp/models.tar.gz -C state models
  gh release view models-latest >/dev/null 2>&1 || gh release create models-latest --title "models" --notes "نماذج مدرّبة — تُستبدل تلقائيًا"
  gh release upload models-latest /tmp/models.tar.gz --clobber
  ;;
*) echo "usage: state.sh pull|push|upload-models"; exit 1;;
esac
