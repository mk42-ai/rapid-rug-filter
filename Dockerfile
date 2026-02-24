FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PORT=3000

EXPOSE 3000

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:3000", "--workers", "2", "--timeout", "30"]
