# Базовый образ с Python (slim — лёгкий, но с нужными инструментами)
FROM python:3.11-slim

# Устанавливаем FFmpeg и другие системные зависимости
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*  # Очистка для уменьшения размера образа

# Устанавливаем зависимости Python
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код бота
COPY . .

# Команда запуска бота (замените на вашу, например python gg.py)
CMD ["python", "your_bot_file.py"]  # <-- Укажите здесь имя вашего главного файла