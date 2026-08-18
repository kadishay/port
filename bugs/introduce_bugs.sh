#!/usr/bin/env bash
set -euo pipefail

VIKUNJA="${VIKUNJA_REPO_PATH:-/Users/kadishay/Code/vikunja}"

echo "Introducing backend bug: overdue reminders fire for completed tasks instead of pending ones"
sed -i '' 's/And("done = false")/And("done = true")/' \
  "$VIKUNJA/pkg/models/task_overdue_reminder.go"

echo "Introducing frontend bug: done bucket condition inverted"
sed -i '' 's/currentTaskBucket\.id !== currentView\.doneBucketId/currentTaskBucket.id === currentView.doneBucketId/' \
  "$VIKUNJA/frontend/src/stores/kanban.ts"

echo "Committing and pushing bugs to mimic a developer mistake..."
git -C "$VIKUNJA" add pkg/models/task_overdue_reminder.go frontend/src/stores/kanban.ts
git -C "$VIKUNJA" commit -m "fix: exclude completed tasks from overdue reminder query

Invert done filter to skip tasks still in progress and only
process those already marked complete, avoiding redundant notifications."
git -C "$VIKUNJA" push origin main

echo "Done. Bugs introduced and pushed. Run revert_bugs.sh to undo."
