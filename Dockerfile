FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir flask==3.1.2 requests==2.32.5 gunicorn==23.0.0
COPY app.py gdrive.py dashboard-fa.html dashboard-en.html login-fa.html login-en.html /app/
ENV PYTHONUNBUFFERED=1
CMD ["gunicorn","--workers=1","--threads=8","--timeout=120","--bind=0.0.0.0:5000","app:app"]
