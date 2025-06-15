FROM python:3.8-slim

WORKDIR /app

# Копируем файлы зависимостей
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код приложения
COPY app_improved.py .

# Создаем директорию для хранения изображений
RUN mkdir -p storage/task_images

# Открываем порт
EXPOSE 5000

# Запускаем приложение через uwsgi
CMD ["uwsgi", "--http", "0.0.0.0:5000", "--module", "app_improved:app"]
