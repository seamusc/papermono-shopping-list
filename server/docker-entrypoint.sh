#!/bin/sh
# Resolves config.env (the config contract: ${VAR:-default} / ${VAR:?message}) against the real
# environment before the app starts. A missing required var stops the container right here with
# the message from config.env, instead of failing later -- and less clearly -- inside the app.
set -eu
set -a
. /app/config.env
set +a
exec "$@"
