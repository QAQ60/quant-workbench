FROM python:3.12-slim
WORKDIR /app
COPY web/ /app/web/
COPY backend/server.py /app/server.py
ENV PORT=8080 WEB_DIR=/app/web TZ=Asia/Shanghai
EXPOSE 8080
CMD ["python3", "/app/server.py"]
