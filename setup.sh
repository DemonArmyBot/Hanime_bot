#!/bin/bash
set -e  # Exit on any error

echo "🚀 Starting Hanime Bot Setup..."

# Check if we're in the right directory
if [ ! -f "requirements.txt" ]; then
    echo "❌ requirements.txt not found. Please run this script from the project root."
    exit 1
fi

echo "📦 Installing Python dependencies..."
pip install --no-cache-dir -r requirements.txt

echo "🌐 Installing Playwright browser..."
playwright install chromium

echo "🔌 Installing Hanime TV plugin..."
python install_plugins.py

echo "✅ Setup completed successfully!"
echo ""
echo "📝 Available commands:"
echo "   python bot.py          - Start the bot"
echo "   gunicorn web:app       - Start the web server"
echo ""
echo "🎉 Your Hanime Bot is ready to use!"
