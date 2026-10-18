#!/bin/sh
# Update MediaForge Web on the server from GitHub. Run it in this folder on gaia:
#   ./deploy.sh          refuses to run while an encode is in progress
#   ./deploy.sh --force  updates anyway (running encodes are lost: jobs live in memory)
set -e
cd "$(dirname "$0")"

if [ "$1" != "--force" ]; then
    jobs=$(curl -s --max-time 5 localhost:5050/api/jobs || echo "[]")
    if [ "$jobs" != "[]" ] && [ -n "$jobs" ]; then
        echo "A conversion is running: $jobs"
        echo "Wait for it to finish, or use ./deploy.sh --force"
        exit 1
    fi
fi

git pull --ff-only
GIT_COMMIT=$(git rev-parse HEAD)
GIT_DATE=$(git log -1 --format=%cI)
export GIT_COMMIT GIT_DATE
docker compose up -d --build
echo "Now running $(git rev-parse --short HEAD)"
