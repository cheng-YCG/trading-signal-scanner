FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 每5分钟运行一次扫描（由 fly.io 持续运行）
# 首次立即执行，之后每300秒一轮
CMD ["sh", "-c", "while true; do python scanner.py; sleep 300; done"]
