# Running in Docker

A `Dockerfile` and `docker-compose.example.yml` are included, aimed at a NAS
with Docker support, though any host works the same.

```bash
cp docker-compose.example.yml docker-compose.yml
# edit the two host paths: your config dir and your NAS media path
```

The `setup` wizard needs a browser, which a headless container doesn't have.
Run it on a machine with a display, then copy what it wrote into the folder
you're about to mount:

```bash
gopro-dl setup --dest /path/that/matches/your/GOPRO_DEST
cp ~/Library/Application\ Support/gopro-dl/{token,config.env} ./gopro-dl-config/
docker compose up -d --build
```

That `config.env` records the *host's* token path, which doesn't exist inside
the container — the compose example's `GOPRO_TOKEN_FILE: /config/token`
overrides it, since an environment variable beats the file. Keep that line, or
delete `GOPRO_TOKEN_FILE` from the copy you mount.

By default the container runs `gopro-dl sync --non-interactive` once and exits
— though `restart: unless-stopped` then starts it again, so for a genuine
one-shot run set `restart: "no"` as well.

Set `GOPRO_DL_CRON_SCHEDULE` (5-field cron, e.g. `"0 3 * * *"`) to have it
install that as a cron job and stay running as a scheduler; `docker compose
logs -f` shows each run. That's a container-only setting read by the entrypoint,
not one of `gopro-dl`'s own `GOPRO_*` variables — everything else is configured
via compose environment variables or the `config.env` you copied in
(`.env.example` lists every key).

Images are published per release, never per commit: a `v0.1.1` tag produces
`:0.1.1`, `:0.1` and `:latest`, so `:latest` is the newest *release* and pinning
`:0.1.1` gets you exactly what the changelog describes — worth doing for a NAS
that runs unattended.

No public prebuilt image exists: `docker-publish.yml` pushes to whatever account
the repo's own `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` secrets name, so a fork
without them fails at the login step. Build locally as above, or set those two
secrets on your fork.

---

[← Documentation index](README.md) · [Project README](../README.md)
