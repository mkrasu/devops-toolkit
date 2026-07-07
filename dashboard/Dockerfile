# SPDX-License-Identifier: MIT
FROM python:3.13-slim

# Run as a non-root user; the dashboard only ever reads the data volume.
RUN useradd --create-home --uid 1000 dashboard
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py store.py ./
COPY templates/ templates/
COPY static/ static/

USER dashboard
ENV DASHBOARD_DATA_DIR=/data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
