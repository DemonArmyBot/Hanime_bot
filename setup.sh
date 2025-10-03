#!/bin/bash
echo "Installing Hanime Bot..."

# Install Python dependencies
pip install -r requirements.txt

# Install Playwright browser
playwright install chromium

# Try to install hanime plugin
python install_plugins.py

echo "Setup complete!"
