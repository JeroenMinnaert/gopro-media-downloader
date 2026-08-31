# Documentation

| Page | What's in it |
| --- | --- |
| [Getting a token](getting-a-token.md) | The `setup` wizard, where the token and browser profile are stored, and how to grab the cookie by hand |
| [Troubleshooting](troubleshooting.md) | Expired tokens, `database is locked`, free-space refusals, wrong dates in Photos, `unverified` files, slow transfers |
| [How it stays safe](reliability.md) | Manifest-first enumeration, two-level resume, pre-flight, circuit breaker, filename collisions |
| [File integrity](file-integrity.md) | Size and content verification, and how the S3 multipart ETag is derived and checked |
| [Capture dates](capture-dates.md) | `fix-dates`: what it writes and why, JPEG rebuild vs MP4 in-place patch, and the camera-clock-reset case |
| [Downloading to a NAS](nas.md) | SMB/NFS destinations, why the manifest goes local, and working in batches on a small disk |
| [Running in Docker](docker.md) | The bundled `Dockerfile` and compose example, headless setup, scheduled runs |
| [API notes](api-notes.md) | The GoPro API's sharp edges — every one of these caused a bug that looked like success |

How the code is put together: [ARCHITECTURE.md](../ARCHITECTURE.md).
Contributing and development setup: [CONTRIBUTING.md](../CONTRIBUTING.md).
Reporting a vulnerability: [SECURITY.md](../SECURITY.md).
