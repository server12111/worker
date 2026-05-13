FROM python:3.11-slim
WORKDIR /app
RUN mkdir -p /app/data && chmod 777 /app/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 3000
CMD ["python", "worker_api.py"]
