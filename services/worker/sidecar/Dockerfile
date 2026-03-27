FROM python:3.12-alpine

RUN apk add --no-cache \
    git \
    curl \
    ca-certificates \
    bash

RUN curl -Lk 'https://code.visualstudio.com/sha/download?build=stable&os=cli-alpine-x64' \
    | tar -xz -C /usr/local/bin

# Python dependencies
RUN pip install --no-cache-dir fastapi uvicorn
RUN pip install --no-cache-dir git+https://github.com/iwaag/agpy.git@main
COPY config/gitconfig /root/.gitconfig
COPY scripts/entrypoint.sh /scripts/entrypoint.sh
RUN chmod +x /scripts/entrypoint.sh

COPY main.py /app/main.py

WORKDIR /workspace

ENTRYPOINT ["/scripts/entrypoint.sh"]
