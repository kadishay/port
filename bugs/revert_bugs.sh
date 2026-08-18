#!/usr/bin/env bash
set -euo pipefail

VIKUNJA="${VIKUNJA_REPO_PATH:-/Users/kadishay/Code/vikunja}"

echo "Reverting backend bug"
sed -i '' 's/nextMinute\.Add(time\.Hour\*38)/nextMinute.Add(time.Hour*14)/' \
  "$VIKUNJA/pkg/models/task_overdue_reminder.go"

echo "Reverting frontend bug"
sed -i '' 's/currentTaskBucket\.id === currentView\.doneBucketId/currentTaskBucket.id !== currentView.doneBucketId/' \
  "$VIKUNJA/frontend/src/stores/kanban.ts"

echo "Done. Bugs reverted."
