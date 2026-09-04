FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir "fastapi>=0.110,<1.0" "uvicorn[standard]>=0.29" "python-multipart>=0.0.9"

COPY src ./kk_server

ENV KK_PORT=8443 \
    KK_DB_PATH=/data/kk-server.db
VOLUME /data
EXPOSE 8443

CMD ["python", "-m", "kk_server"]
