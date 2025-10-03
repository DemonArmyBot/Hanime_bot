from flask import Flask, jsonify
import threading, time, os, signal, sys

app = Flask(__name__)

last_ping = time.time()
SLEEP_TIMEOUT = int(os.getenv("SLEEP_TIMEOUT", "600"))  # 10 min default

@app.route("/")
def home():
    return "Hanime Bot is running!"

@app.route("/ping")
def ping():
    global last_ping
    last_ping = time.time()
    return jsonify({"status": "alive"})

def monitor_idle():
    global last_ping
    while True:
        if time.time() - last_ping > SLEEP_TIMEOUT:
            print("⚡ No pings, shutting down to save resources.")
            os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(30)

if __name__ == "__main__":
    threading.Thread(target=monitor_idle, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))