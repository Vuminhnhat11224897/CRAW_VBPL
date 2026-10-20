FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=Asia/Ho_Chi_Minh
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY legal_crawler legal_crawler
COPY config config
# Drafts only: this image has no code path that writes to Elasticsearch. See docs/adr/0001-v3-cluster-v2-schema.md
CMD ["python", "-m", "legal_crawler.nightly"]
