#!/usr/bin/env bash
set -euo pipefail

VIKUNJA="${VIKUNJA_REPO_PATH:-/Users/kadishay/Code/vikunja}"

echo "Introducing backend bug: overdue window expanded from 14h to 38h"
sed -i '' 's/nextMinute\.Add(time\.Hour\*14)/nextMinute.Add(time.Hour*38)/' \
  "$VIKUNJA/pkg/models/task_overdue_reminder.go"

echo "Introducing frontend bug: done bucket condition inverted"
sed -i '' 's/currentTaskBucket\.id !== currentView\.doneBucketId/currentTaskBucket.id === currentView.doneBucketId/' \
  "$VIKUNJA/frontend/src/stores/kanban.ts"

echo "Done. Run revert_bugs.sh to undo."
