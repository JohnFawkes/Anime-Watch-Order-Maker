FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir "pip>=26.0" && \
    pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data && \
    addgroup --system app && adduser --system --ingroup app app && \
    chown -R app:app /data /app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
