import paho.mqtt.client as mqtt
import sqlite3
import json
import datetime
import os
import threading

from flask import Flask, jsonify, render_template
from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---
def get_env_int(name, default):
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        return default


def get_env_topics(default_topics):
    value = os.getenv("MQTT_TOPICS")
    if not value:
        return default_topics

    topics = [topic.strip() for topic in value.split(",") if topic.strip()]
    return topics or default_topics


MQTT_BROKER = os.getenv("MQTT_BROKER", "mqtt-broker.local")
MQTT_PORT = get_env_int("MQTT_PORT", 1883)
DATABASE_NAME = os.getenv("DATABASE_NAME", "device_data.db")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = get_env_int("WEB_PORT", 8000)

TOPICS = get_env_topics([
    "stat/+/init",
    "tele/+/INFO2",
    "wled/+/state",
    "home/device/+/info",
])

app = Flask(__name__)

def get_device_id_from_topic(topic):
    """
    Extracts the core device ID from the MQTT topic. 
    Assumes the device ID is the key differentiator (usually the 2nd or 3rd element).
    """
    parts = topic.split('/')
    
    if "stat/" in topic:
        # For stat/+/init, the device ID is typically index 1
        if len(parts) > 1:
             return parts[1]
    
    elif "tele/" in topic:
        # For tele/+/INFO2, the device ID is typically index 1
        if len(parts) > 1:
             return parts[1]
    
    elif "wled/" in topic:
        # For wled/+/state, the device ID is typically index 1
        if len(parts) > 1:
             return parts[1]
    
    elif "home/device/" in topic:
        # For home/device/+/info, the device ID is typically index 2
        if len(parts) > 2:
             return parts[2]

    # Fallback
    return parts[0] if parts else "UNKNOWN_DEVICE"


def initialize_database():
    """Connects to the SQLite database and ensures the table exists."""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS device_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_name TEXT NOT NULL,
            ip_address TEXT NOT NULL,
            hostname TEXT,
            timestamp TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    print(f"✅ Database initialized: {DATABASE_NAME}")


def get_db_connection():
    conn = sqlite3.connect(DATABASE_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def write_to_db(device, ip, hostname):
    """Connects to the database and inserts the new record."""
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO device_details (device_name, ip_address, hostname, timestamp) VALUES (?, ?, ?, ?)",
                (device, ip, hostname, timestamp)
            )
            conn.commit()
        print(f"\n🎉 [DB WRITE] Device '{device}' updated: IP={ip}, Host={hostname}")
    except sqlite3.Error as e:
        print(f"❌ Database error occurred during write: {e}")


def fetch_latest_devices():
    query = """
        SELECT id, device_name, ip_address, hostname, timestamp
        FROM device_details
        WHERE id IN (
            SELECT MAX(id)
            FROM device_details
            GROUP BY device_name
        )
        ORDER BY timestamp DESC, id DESC
    """
    with get_db_connection() as conn:
        rows = conn.execute(query).fetchall()

    return [dict(row) for row in rows]


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/devices")
def list_devices():
    devices = fetch_latest_devices()
    return jsonify(
        {
            "devices": devices,
            "count": len(devices),
            "generated_at": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
    )


@app.get("/health")
def health_check():
    return jsonify({"status": "ok"})

def on_message(client, userdata, msg):
    """The callback function executed when a message is received."""
    topic = msg.topic
    payload = msg.payload.decode('utf-8')
    device = get_device_id_from_topic(topic)
    
    print(f"\n[RECV] Topic: {topic} | Device: {device} | Payload: {payload[:70]}...")
    
    ip = "N/A"
    hostname = "N/A"
    
    try:
        data = json.loads(payload)
        
        if topic.startswith("stat/"):
            # --- 1. Original stat/+/init handler (MAC, IP, other data) ---
            # We assume the payload structure is a dictionary containing these keys
            ip = data.get('ip', 'N/A') 
            hostname = data.get('hostname', 'N/A') 
            
        elif topic.startswith("tele/"):
            # --- 2. Original tele/+/INFO2 handler (Hostname, IP) ---
            info2 = data.get("Info2", {})
            ip = info2.get("IPAddress", "N/A")
            hostname = info2.get("Hostname", "N/A")

        elif topic.startswith("wled/"):
            # --- 3. WLED MQTT State Handler ---
            # WLED often publishes status updates. We look for an 'ip' attribute.
            # Assuming the state update dictionary contains or relates to IP/Hostname.
            # If WLED is used as a gateway, its primary IP might be available in the payload:
            if isinstance(data, dict) and data.get('ip'):
                ip = data['ip']
                # If WLED doesn't provide a specific hostname, use a generic identifier
                hostname = "WLED_LIGHT" 
            else:
                # Fallback if IP is not directly available in the state payload
                ip = "N/A (WLED State)"
                hostname = "WLED_LIGHT"

        elif topic.startswith("home/device/"):
            # --- 4. Generic HomeKit/Device Info Handler ---
            # These devices might use attributes, metadata, or nested structures.
            # We look for common keys like 'ip' or 'address'.
            
            # Example: If the payload is flat, look for 'ip' and 'hostname' keys
            if isinstance(data, dict):
                ip = data.get('ip', 'N/A')
                hostname = data.get('hostname', 'N/A')
            else:
                # If the payload is a list or other structure, skip or refine parsing
                pass
        
        else:
            print(f"⚠️ No specific parser found for topic: {topic}")
            return
            
        # Write to DB only if we successfully extracted device name (handled by get_device_id_from_topic)
        write_to_db(device, ip, hostname)
            
    except json.JSONDecodeError:
        print(f"Skipping payload: Could not decode JSON for topic {topic}.")
    except Exception as e:
        print(f"An critical error occurred processing message: {e}")


def run_mqtt_listener():
    client = mqtt.Client(client_id="python_device_logger")
    client.on_message = on_message
    
    print(f"🔌 Attempting to connect to MQTT Broker at {MQTT_BROKER}:{MQTT_PORT}...")
    
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        
        # 3. Subscribe to topics
        for topic in TOPICS:
            client.subscribe(topic)
            print(f"✅ Subscribed to topic: {topic}")

        client.loop_forever()

    except ConnectionRefusedError:
        print("❌ FATAL: Connection refused. Ensure the MQTT broker is running and accessible.")
    except Exception as e:
        print(f"❌ An unexpected error occurred: {e}")


def main():
    initialize_database()

    mqtt_thread = threading.Thread(target=run_mqtt_listener, name="mqtt-listener", daemon=True)
    mqtt_thread.start()

    print(f"🌐 Starting web UI on {WEB_HOST}:{WEB_PORT}")
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
