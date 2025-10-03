FROM python:3.13-slim

# Install system dependencies including git and ffmpeg
RUN apt-get update && \
    apt-get install -y \
    git \
    ffmpeg \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright dependencies
RUN apt-get update && \
    apt-get install -y \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxcb1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libxcomposite1 \
    libx11-xcb1 \
    libxkbcommon0 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser
RUN playwright install chromium

# Copy bot files
COPY bot.py web.py ./

# Copy and set up installation scripts
COPY requirements.txt install_plugins.py setup.sh ./

# Make setup script executable and run it
RUN chmod +x setup.sh && ./setup.sh

# Expose port for web server
EXPOSE $PORT

# Start both bot and web server
CMD gunicorn -w 1 -b 0.0.0.0:$PORT web:app & python bot.py
