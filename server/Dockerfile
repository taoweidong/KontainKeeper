FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY kk_server ./kk_server
COPY web ./web

ENV KK_PORT=8443 \
    KK_DB_PATH=/data/kk-server.db
VOLUME /data
EXPOSE 8443

CMD ["python", "-m", "kk_server"]
