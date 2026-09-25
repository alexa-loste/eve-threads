#!/bin/sh
# Start the eve-threads sidecar on localhost:8799
cd "$(dirname "$0")"
exec .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8799
