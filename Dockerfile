FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bundle the chart and zip libraries so the dashboard doesn't depend on a public CDN.
RUN mkdir -p /app/vendor && python -c "import urllib.request as u; \
u.urlretrieve('https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js', '/app/vendor/chart.umd.js'); \
u.urlretrieve('https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js', '/app/vendor/jszip.min.js')"

COPY app/ /app/app/
RUN mkdir -p /data && chmod 777 /data && chmod -R a+rX /app

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
