#!/bin/sh
# If GOPRO_DL_CRON_SCHEDULE is set, install it as a cron job that runs
# `gopro-dl sync --non-interactive` on that schedule and stay running in the
# foreground -- the container becomes a long-lived scheduler instead of a
# one-shot run. Otherwise, just run the image's CMD once and exit, same as
# any normal container.
set -eu

if [ -n "${GOPRO_DL_CRON_SCHEDULE:-}" ]; then
    # cron drops the container's own environment before running a job, so
    # a /etc/cron.d file gets its own VAR=value lines (same format as
    # /etc/crontab) rather than relying on it to inherit anything -- most
    # importantly PATH, or `gopro-dl` (installed under /usr/local/bin)
    # isn't found. Env vars gopro-dl itself reads (GOPRO_DEST, ...) go
    # through the same mechanism.
    {
        echo "PATH=$PATH"
        env | grep '^GOPRO_' | grep -v '^GOPRO_DL_CRON_SCHEDULE='
        # The schedule always runs a fixed, non-interactive sync -- not
        # whatever CMD/args this container happened to be started with,
        # which would otherwise have to be re-quoted into a single line.
        echo "${GOPRO_DL_CRON_SCHEDULE} root gopro-dl sync --non-interactive >/proc/1/fd/1 2>/proc/1/fd/2"
    } > /etc/cron.d/gopro-dl
    chmod 0644 /etc/cron.d/gopro-dl

    echo "gopro-dl: scheduled '${GOPRO_DL_CRON_SCHEDULE}' via cron, staying in the foreground."
    exec cron -f
fi

exec gopro-dl "$@"
