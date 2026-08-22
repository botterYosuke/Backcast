FROM python:3.11-slim
ENV PYTHONUNBUFFERED=True
ENV STOCKDATA_CACHE_DIR=/cache
ENV PORT=8080

# Build context is the repository root (main.py imports `src/tickreplay`),
# not this directory — see docs/tick-replay.md for the exact build command.
WORKDIR /app/cloud-run
COPY cloud-run/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cloud-run/main.py .
COPY src/ /app/src/

# --workers must stay 1: main.py and the tickreplay app it mounts both keep
# in-process module-level state (the OperationTracker/repository singleton),
# which a second worker process would not share.
CMD exec gunicorn --bind :$PORT --workers 1 --timeout 0 -k uvicorn.workers.UvicornWorker main:app
