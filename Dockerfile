FROM python:3.14-alpine

# Set environment variables to optimize Python for Docker
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# UID/GID from .env (default 1000)
ARG PUID
ARG PGID

# Set the working directory inside the container
WORKDIR /app

# Install Python dependencies (build deps added temporarily for any source builds).
# requirements.txt is copied first so this layer is cached unless deps change.
COPY requirements.txt .
RUN apk add --no-cache --virtual .build-deps \
        build-base \
        libffi-dev \
    && pip install --no-cache-dir -r requirements.txt \
    && apk del .build-deps

# Copy the source code from the bot/ directory to /app
COPY bot/ .

# Create app user with host UID/GID and data directory
RUN addgroup -g "$PGID" appuser && \
    adduser -u "$PUID" -G appuser -D appuser && \
    mkdir -p /app/data && \
    chown -R "$PUID:$PGID" /app

USER appuser

CMD ["python", "bot.py"]
