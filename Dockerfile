# Clean-machine check: build the wheel, install it (not editable) into a fresh python:3.13-slim,
# run the test suite against the installed package, then a short vf-eval.
# Build from a fresh `git clone` of the repo, never the working directory — see DOCKER.md.
# OPENAI_API_KEY is passed at runtime with -e; it is never part of the image.

# ---------- stage 1: build the wheel and export the locked dependencies ----------
FROM python:3.13-slim AS build
RUN pip install --no-cache-dir uv
WORKDIR /src
COPY environments/invoice_approval/ ./
# --locked fails if uv.lock is out of date with pyproject.toml; dev group included for pytest.
RUN uv build --wheel -o /dist \
 && uv export --locked --no-emit-project --group dev -o /dist/requirements.txt

# ---------- stage 2: a clean interpreter with only the wheel and its locked dependencies ----------
FROM python:3.13-slim
COPY --from=build /dist /dist
RUN pip install --no-cache-dir -r /dist/requirements.txt \
 && pip install --no-cache-dir --no-deps /dist/*.whl \
 && pip check

# Tests and the task generator they import, outside site-packages; no source of the package itself.
WORKDIR /work
COPY environments/invoice_approval/tests/ tests/
COPY environments/invoice_approval/scripts/ scripts/
RUN test ! -e ap && test ! -e invoice_approval.py

ENV PYTHONDONTWRITEBYTECODE=1
CMD set -e; \
    python -c "import ap, invoice_approval, sys; f = invoice_approval.__file__; print('package:', f); sys.exit('site-packages' not in f or 'site-packages' not in ap.__file__)"; \
    python -m pytest -q -p no:cacheprovider tests; \
    vf-eval invoice-approval -m gpt-4.1-mini -b https://api.openai.com/v1 -k OPENAI_API_KEY \
        -n 3 -r 1 -c 4 -s -C stop_condition -d --output-dir /work/outputs
