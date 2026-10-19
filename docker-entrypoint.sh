#!/bin/sh
# Run·dere container entrypoint (YONDER-293).
#
# The app runs as an unprivileged user (uid/gid 10001), never root. Volumes made
# by earlier images belong to root, so when the container starts as root this
# hands the app's data folders to that user once (only when a folder isn't
# already theirs), then drops privileges for good with setpriv. Started with
# `user: 10001:10001` it skips straight to the app.
set -e
umask 027                                   # stored files aren't world-readable

APP_ID=10001

own() {
    d="$1"
    [ -n "$d" ] || return 0
    mkdir -p "$d" 2>/dev/null || true
    [ -d "$d" ] || return 0
    if [ "$(stat -c %u "$d" 2>/dev/null)" != "$APP_ID" ]; then
        echo "rundere: handing $d to the app user (one time)"
        chown -R "$APP_ID:$APP_ID" "$d" 2>/dev/null \
            || echo "rundere: couldn't change the owner of $d (a network share?); make it writable by uid $APP_ID"
    fi
}

if [ "$(id -u)" != "0" ] && [ ! -w "${HOME:-/nonexistent}" ]; then
    # Run as another uid (`user: "1000:1000"`), the image's home isn't theirs:
    # give caches (fontconfig, for the PDF export) a writable home instead.
    export HOME="/tmp/rundere-home-$(id -u)"
    mkdir -p "$HOME"
fi

if [ "$(id -u)" = "0" ]; then
    own "${TX_ARTIFACTS_DIR:-/data/artifacts}"
    own "${TX_MAP_TILE_CACHE_DIR:-}"
    own "${TX_DOCUMENTS_DIR:-}"
    case "${TX_DATABASE_URL:-}" in            # a SQLite database (the demo) lives in a folder too
        sqlite:////*) own "$(dirname "${TX_DATABASE_URL#sqlite:///}")" ;;
    esac
    exec setpriv --reuid="$APP_ID" --regid="$APP_ID" --clear-groups -- "$@"
fi
exec "$@"
