app := "shopping-list"

default:
    @just --list

# Run lint and the server tests (the Dockerfile's `test` stage).
test:
    docker build --target test --quiet server >/dev/null && echo tests passed

# Build the server image locally (nothing is pushed or deployed).
build:
    docker build -t {{app}}:local server

# Build the firmware in a container into firmware/dist/<version>.bin. Settings come from SHOPPING_LIST_* env vars or -c FILE.
firmware *args:
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v deploy-tools >/dev/null && [ -z "${SHOPPING_LIST_SERVER_URL:-}" ] && [[ " {{args}} " != *" -c "* ]]; then
        exec deploy-tools with-secrets {{app}} firmware/builder/build.sh {{args}}
    fi
    exec firmware/builder/build.sh {{args}}

[private]
need-tools:
    @command -v deploy-tools >/dev/null || { echo "deploy-tools not found: deployment tooling is private (test, build and firmware work without it)" >&2; exit 1; }

# deploy-tools builds the current directory, so the recipes below run it from server/, where the Dockerfile is.

# Build this working tree as a dev-* image and run it on the VM as an override (git is untouched). Optional: an existing tag.
try *tag: need-tools
    cd server && deploy-tools try {{app}} {{tag}}

# Build the current commit (clean and pushed) as <sha> and push it to the registry.
release: need-tools
    cd server && deploy-tools release {{app}}

# Pin this commit (or the given tag) as the version to run; then `just deploy`.
promote *tag: need-tools
    cd server && deploy-tools promote {{app}} {{tag}}

# Deploy the pinned version; also clears an override.
deploy *args: need-tools
    deploy-tools deploy {{app}} {{args}}

revert: need-tools
    deploy-tools revert {{app}}

status: need-tools
    deploy-tools status {{app}}

logs *n: need-tools
    deploy-tools logs {{app}} {{n}}
