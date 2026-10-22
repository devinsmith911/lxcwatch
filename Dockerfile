FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 lxcwatch \
    && install -d -o lxcwatch -g lxcwatch /data

WORKDIR /app
COPY pyproject.toml ./
COPY lxcwatch ./lxcwatch
RUN pip install --no-cache-dir .

USER lxcwatch
ENV LXCWATCH_DATA_DIR=/data
VOLUME /data
EXPOSE 8080
CMD ["uvicorn", "lxcwatch.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--no-access-log"]
