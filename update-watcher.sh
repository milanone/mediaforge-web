#!/bin/sh
# Host-side half of the in-page "Aggiorna ora" button. Run it every minute from cron on the server:
#   * * * * * /home/francesco/docker/mediaforge-web/update-watcher.sh
# It touches updates/alive (the page shows the button only while this is fresh) and, when the page has left
# updates/request, runs deploy.sh (which refuses if a conversion is running). The container itself has no
# access to Docker: it only writes the request file.
cd "$(dirname "$0")" || exit 1
mkdir -p updates
touch updates/alive
[ -f updates/request ] || exit 0
rm -f updates/request
echo "aggiornamento in corso ($(date +%H:%M:%S))" > updates/status
if ./deploy.sh >> updates/deploy.log 2>&1; then
    echo "completato ($(date +%H:%M:%S))" > updates/status
else
    echo "non riuscito, vedi updates/deploy.log" > updates/status
fi
