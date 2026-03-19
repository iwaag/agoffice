FROM node:20-bookworm-slim

ENV NOOB_SESSION_ROOT=/mnt/session \
    NOOB_AGENT_COMMAND=pi-coding-agent

WORKDIR /app

RUN npm install -g @mariozechner/pi-coding-agent

COPY ./services/worker_noob/noob_runner.mjs /app/noob_runner.mjs
COPY ./services/worker_noob/entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
