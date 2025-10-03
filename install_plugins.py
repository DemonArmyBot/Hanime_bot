#!/usr/bin/env python3
import os
import sys
import subprocess
import site
from pathlib import Path
import importlib.util

def install_hanime_plugin():
    """Install hanime-tv-plugin manually"""
    print("🔧 Installing Hanime TV plugin...")
    
    try:
        # Clone the repository
        print("📥 Cloning hanime-tv-plugin repository...")
        result = subprocess.run([
            'git', 'clone', 'https://github.com/cynthia2006/hanime-tv-plugin.git',
            '/tmp/hanime-tv-plugin'
        ], capture_output=True, text=True, check=True)
        
        print("✅ Repository cloned successfully")
        
        # Check if requirements.txt exists in the plugin
        plugin_req_path = Path('/tmp/hanime-tv-plugin/requirements.txt')
        if plugin_req_path.exists():
            print("📦 Installing plugin dependencies...")
            subprocess.run([
                sys.executable, '-m', 'pip', 'install', '-r',
                str(plugin_req_path)
            ], check=True)
        
        # Add to Python path
        plugin_path = '/tmp/hanime-tv-plugin'
        if plugin_path not in sys.path:
            sys.path.insert(0, plugin_path)
        
        # Test if the plugin can be imported
        try:
            spec = importlib.util.find_spec('yt_dlp_plugins.hanime_tv')
            if spec is not None:
                print("✅ Hanime TV plugin installed and importable")
            else:
                print("⚠️ Hanime TV plugin files exist but cannot be imported directly")
                
        except ImportError as e:
            print(f"⚠️ Hanime TV plugin import test failed: {e}")
            print("📋 Plugin files are available at /tmp/hanime-tv-plugin/")
            
        return True
        
    except subprocess.CalledProcessError as e:
        print(f"❌ Git clone failed: {e}")
        print(f"stderr: {e.stderr}")
        return False
    except Exception as e:
        print(f"❌ Installation failed: {e}")
        return False

if __name__ == "__main__":
    success = install_hanime_plugin()
    sys.exit(0 if success else 1)
