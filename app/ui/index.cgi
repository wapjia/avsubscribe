#!/bin/sh

APP_ROOT="${AVSUB_APP_ROOT:-/var/apps/AVSubscribe/target}"
APP_STATE="${TRIM_PKGVAR:-/var/apps/AVSubscribe/var}"
if [ -e "$APP_STATE/disabled" ]; then
  printf 'Status: 503 Service Unavailable\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n应用当前已停用'
  exit 0
fi
URI_NO_QUERY="${REQUEST_URI%%\?*}"
REL_PATH="${PATH_INFO:-/}"
case "$URI_NO_QUERY" in
  *index.cgi*) REL_PATH="${URI_NO_QUERY#*index.cgi}" ;;
esac
case "$REL_PATH" in
  /api/*) PATH_INFO="$REL_PATH"; export PATH_INFO; exec python3 "$APP_ROOT/bin/app.py" ;;
  /vendor/*) PATH_INFO="$REL_PATH"; export PATH_INFO; exec python3 "$APP_ROOT/bin/app.py" ;;
  ""|/|/index.html) FILE="index.html" ;;
  /style.css) FILE="style.css" ;;
  /app.js) FILE="app.js" ;;
  /version.js) FILE="version.js" ;;
  *) printf 'Status: 404 Not Found\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nNot Found'; exit 0 ;;
esac
case "$FILE" in
  *.html) CONTENT_TYPE="text/html; charset=utf-8" ;;
  *.css) CONTENT_TYPE="text/css; charset=utf-8" ;;
  *.js) CONTENT_TYPE="application/javascript; charset=utf-8" ;;
esac
if [ ! -f "$APP_ROOT/www/$FILE" ]; then
  printf 'Status: 404 Not Found\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nNot Found'
  exit 0
fi
printf 'Content-Type: %s\r\nCache-Control: no-store\r\n\r\n' "$CONTENT_TYPE"
cat "$APP_ROOT/www/$FILE"
