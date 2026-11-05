FROM python:3.15-slim
# Pull in Debian security fixes that landed after the base image was built
# (e.g. apt/libapt-pkg CVE-2026-109518), so Trivy doesn't flag fixable OS CVEs.
RUN apt-get update && \
    apt-get upgrade -y --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
# pip is only needed at build time. Its vendored copies of urllib3, msgpack
# and setuptools (pip/_vendor) get flagged by Trivy, so remove pip (and the
# ensurepip bundled wheels) from the runtime image once dependencies are in.
RUN pip install --no-cache-dir -r requirements.txt && \
    pip uninstall -y pip && \
    rm -rf /usr/local/lib/python3*/ensurepip
COPY . .
RUN mkdir -p /data && \
    addgroup --system app && adduser --system --ingroup app app && \
    chown -R app:app /data /app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
