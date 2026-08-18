#!/usr/bin/env bash
set -euo pipefail

VIKUNJA="${VIKUNJA_REPO_PATH:-/Users/kadishay/Code/vikunja}"

echo "Introducing backend bug: overdue window expanded from 14h to 38h"
sed -i '' 's/nextMinute\.Add(time\.Hour\*14)/nextMinute.Add(time.Hour*38)/' \
  "$VIKUNJA/pkg/models/task_overdue_reminder.go"

echo "Introducing frontend bug: done bucket condition inverted"
sed -i '' 's/currentTaskBucket\.id !== currentView\.doneBucketId/currentTaskBucket.id === currentView.doneBucketId/' \
  "$VIKUNJA/frontend/src/stores/kanban.ts"

echo "Committing and pushing bugs to mimic a developer mistake..."
git -C "$VIKUNJA" add pkg/models/task_overdue_reminder.go frontend/src/stores/kanban.ts
git -C "$VIKUNJA" commit -m "fix: adjust overdue reminder window and kanban done bucket logic

Increase the overdue check window to better catch tasks approaching
their deadline. Also tighten the kanban done-bucket move condition
to avoid redundant state updates."
git -C "$VIKUNJA" push origin main

echo "Done. Bugs introduced and pushed. Run revert_bugs.sh to undo."
