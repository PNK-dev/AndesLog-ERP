FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["sh", "-c", "flask --app app:create_app db upgrade && flask --app app:create_app seed-admin && gunicorn --workers 2 --bind 0.0.0.0:8000 'app:create_app()'"]
