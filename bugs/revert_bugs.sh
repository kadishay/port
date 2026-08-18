#!/usr/bin/env bash
set -euo pipefail

VIKUNJA="${VIKUNJA_REPO_PATH:-/Users/kadishay/Code/vikunja}"

echo "Reverting backend bug"
sed -i '' 's/And("done = true")/And("done = false")/' \
  "$VIKUNJA/pkg/models/task_overdue_reminder.go"

echo "Reverting frontend bug"
sed -i '' 's/currentTaskBucket\.id === currentView\.doneBucketId/currentTaskBucket.id !== currentView.doneBucketId/' \
  "$VIKUNJA/frontend/src/stores/kanban.ts"

echo "Committing and pushing revert..."
git -C "$VIKUNJA" add pkg/models/task_overdue_reminder.go frontend/src/stores/kanban.ts
git -C "$VIKUNJA" commit -m "revert: restore correct done filter and kanban done bucket condition"
git -C "$VIKUNJA" push origin main

echo "Done. Bugs reverted and pushed."
