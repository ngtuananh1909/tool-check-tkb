#!/bin/sh
# start.sh - Entry point that properly handles PORT environment variable

PORT="${PORT:-8000}"
exec uvicorn webhook_app:app --host 0.0.0.0 --port "$PORT"
