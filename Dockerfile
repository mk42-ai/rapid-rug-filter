FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV APP_VERSION=4.1.0
ENV PORT=3000
ENV SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=f69e06c4-795c-426c-b55a-dd3982840701

EXPOSE 3000

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:3000", "--workers", "1", "--threads", "4", "--timeout", "30"]
