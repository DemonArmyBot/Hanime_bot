FROM python:3.11-slim

# Install ffmpeg for yt-dlp
RUN apt-get update && apt install git && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py web.py ./

# Start both bot and web server
CMD gunicorn -w 1 -b 0.0.0.0:$PORT web:app & python bot.py
