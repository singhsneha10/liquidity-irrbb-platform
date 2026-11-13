# Container image for the daily pipeline (riskplatform-run).
#
# Build:  docker build -t riskplatform .
# Run:    docker run --rm riskplatform                       (synthetic bank; results stay in the container)
#         docker run --rm -e RISKPLATFORM_STORAGE_BACKEND=s3 -e RISKPLATFORM_S3_BUCKET=<bucket> riskplatform
#
# No credentials are baked in: in AWS the task's IAM role supplies them; locally, pass them as
# environment variables at run time. .dockerignore keeps .env, venv/, data/ and .git/ out.

# --- Stage 1: install the project into a separate folder --------------------------------------
FROM python:3.12-slim-trixie AS build
WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

# --- Stage 2: the runtime image (no build tools, no source tree, non-root) ---------------------
FROM python:3.12-slim-trixie
# No .pyc files; logs written straight away (so CloudWatch shows them as they happen).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 riskplatform
COPY --from=build /install /usr/local
WORKDIR /app
COPY config ./config
RUN mkdir data && chown riskplatform:riskplatform data
USER riskplatform

# Exit codes: 0 clean, 1 stopped or error, 2 findings (a breach or an IRRBB outlier).
ENTRYPOINT ["riskplatform-run"]
