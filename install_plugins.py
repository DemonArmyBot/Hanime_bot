#!/usr/bin/env python3
import os
import sys
import subprocess
import site
from pathlib import Path

def install_hanime_plugin():
    """Install hanime-tv-plugin manually"""
    try:
        # Clone the repository
        subprocess.run([
            'git', 'clone', 'https://github.com/cynthia2006/hanime-tv-plugin.git',
            '/tmp/hanime-tv-plugin'
        ], check=True)
        
        # Install dependencies
        subprocess.run([
            sys.executable, '-m', 'pip', 'install', '-r',
            '/tmp/hanime-tv-plugin/requirements.txt'
        ], check=True)
        
        # Add to Python path
        plugin_path = '/tmp/hanime-tv-plugin'
        if plugin_path not in sys.path:
            sys.path.insert(0, plugin_path)
            
        print("✅ Hanime TV plugin installed successfully")
        return True
        
    except Exception as e:
        print(f"❌ Failed to install hanime plugin: {e}")
        return False

if __name__ == "__main__":
    install_hanime_plugin()
