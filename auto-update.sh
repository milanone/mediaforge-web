#!/bin/sh
# Nightly update from cron on the server, e.g.:
#   0 4 * * * /home/francesco/docker/mediaforge-web/auto-update.sh
# If GitHub main is newer than the commit the container was built from, runs deploy.sh (pull + rebuild).
# deploy.sh refuses while a conversion is running: the update is then simply tried again the next night.
# Everything is logged to updates/auto-update.log.
cd "$(dirname "$0")" || exit 1
mkdir -p updates
log=updates/auto-update.log
stamp() { date '+%F %T'; }

if ! git fetch -q origin; then
    echo "$(stamp) git fetch failed, nothing done" >> "$log"
    exit 1
fi
latest=$(git rev-parse origin/main)
running=$(curl -s --max-time 5 localhost:5050/api/version | sed -n 's/.*"commit":"\([0-9a-f]*\)".*/\1/p')
[ "$running" = "$latest" ] && exit 0

echo "$(stamp) updating ${running:-?} -> $latest" >> "$log"
if ./deploy.sh >> "$log" 2>&1; then
    echo "$(stamp) done" >> "$log"
else
    echo "$(stamp) not updated (conversion running or error), will retry tomorrow" >> "$log"
fi
