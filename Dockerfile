FROM python:3.11-slim

WORKDIR /app

# Копируем requirements.txt
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код бота
COPY . .

# Создаем папку для базы данных
RUN mkdir -p /data

# Запуск бота
CMD ["python", "bot.py"]
