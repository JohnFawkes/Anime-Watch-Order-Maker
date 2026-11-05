FROM python:3.15-slim
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
