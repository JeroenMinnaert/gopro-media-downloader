FROM python:3.14-slim

# `cron` is the only extra system package: gopro-dl itself has no native
# dependencies beyond what pip installs. The interactive `setup` wizard's
# browser login isn't meant to run in this image (no display) -- create the
# token/config.env on a machine with one and mount them in instead (see
# README's "Running in Docker" section).
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
WORKDIR /

COPY docker/entrypoint.sh /usr/local/bin/gopro-dl-entrypoint
RUN chmod +x /usr/local/bin/gopro-dl-entrypoint

ENTRYPOINT ["gopro-dl-entrypoint"]
CMD ["sync", "--non-interactive"]
