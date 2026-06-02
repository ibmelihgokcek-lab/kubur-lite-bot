FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY kubur_lite.py .
CMD ["python", "kubur_lite.py"]
