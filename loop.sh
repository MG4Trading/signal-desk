#!/usr/bin/env bash
# Keeps Signal Desk fresh: rebuilds and publishes about every 10 minutes for close to 6 hours,
# then starts the next run so updates never stop. GitHub's own schedule is only a backup,
# because it often runs hours late.
set -u
INTERVAL=${SD_INTERVAL:-600}
LIMIT=${SD_LIMIT:-20700}          # 5 h 45 min
START=$(date +%s)
REMOTE="https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
git config --global user.name "github-actions[bot]"
git config --global user.email "github-actions[bot]@users.noreply.github.com"

while :; do
  T0=$(date +%s)
  # pick up any changes to topics, settings or code
  git fetch -q origin main && git reset -q --hard origin/main
  rm -rf prev site
  git clone -q --depth 1 --branch gh-pages "$REMOTE" prev 2>/dev/null || mkdir -p prev
  if python build.py && [ -f site/index.html ]; then
    ( cd site && git init -q -b gh-pages && git add -A && git commit -qm "Update $(date -u +%FT%H:%MZ)" \
        && git push -qf "$REMOTE" gh-pages ) || echo "publish failed"
  else
    echo "build failed"
  fi
  NOW=$(date +%s)
  if [ $((NOW - START + INTERVAL)) -ge "$LIMIT" ]; then break; fi
  WAIT=$((INTERVAL - (NOW - T0))); [ "$WAIT" -gt 30 ] && sleep "$WAIT"
done

# hand over to a fresh run
gh workflow run update.yml --ref main -R "$GITHUB_REPOSITORY" || echo "could not start the next run"
