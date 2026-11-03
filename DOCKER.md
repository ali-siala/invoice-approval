# Clean-machine check (Docker)

The `Dockerfile` proves the environment works from its built wheel alone. It builds the wheel and
exports the locked dependencies (`uv.lock`), then installs both, not editable, into a fresh
`python:3.13-slim`. It copies in only `tests/` and `scripts/`, which the tests import; no package
source. It runs pytest against the installed package, then a 3-task eval with gpt-4.1-mini.

Always build from a fresh clone of the committed state, never from the working directory, so
uncommitted or gitignored files can't hide a missing piece.
Commit first: the clone sees only committed changes. From the repo root:

```bash
CLONE=$(mktemp -d)
git clone . "$CLONE"
docker build -t invoice-approval-check "$CLONE"
rm -rf "$CLONE"
```

Then run it from the repo root. The API key is passed at runtime only and is never in the image.
Results (`-s`) land in `./docker-outputs` on the host:

```bash
mkdir -p docker-outputs
docker run --rm -e OPENAI_API_KEY -v "$PWD/docker-outputs:/work/outputs" invoice-approval-check
```

`-e OPENAI_API_KEY` without a value forwards the variable from your shell, so the key never shows up
on the command line or in shell history.

The container exits non-zero if the package is imported from anywhere but site-packages, if any test
fails, or if vf-eval fails. A rollout ending in `has_error` is an infrastructure failure (see
CLAUDE.md); run the container again.

The docker commands need access to the Docker socket (membership in the `docker` group).
