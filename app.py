import paho.mqtt.client as mqtt
import sqlite3
import json
import datetime
import os
import threading

from flask import Flask, jsonify, render_template, request
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
MQTT_KEEPALIVE = get_env_int("MQTT_KEEPALIVE", 60)
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


def first_present(*values):
    for value in values:
        if value not in (None, ""):
            return str(value)
    return "N/A"


def parse_stat_init_payload(data, default_device):
    var1 = data.get("var1", {}) if isinstance(data.get("var1"), dict) else {}

    device = first_present(
        data.get("device"),
        data.get("value3"),
        var1.get("value3"),
        default_device,
    )
    ip = first_present(
        data.get("ip"),
        data.get("IPAddress"),
        data.get("value1"),
        var1.get("value1"),
    )
    hostname = first_present(
        data.get("hostname"),
        data.get("mac"),
        data.get("value2"),
        var1.get("value2"),
    )
    return device, ip, hostname


def parse_tele_info2_payload(data, default_device):
    info2 = data.get("Info2", {}) if isinstance(data.get("Info2"), dict) else {}

    device = first_present(
        data.get("device"),
        info2.get("device"),
        default_device,
    )
    ip = first_present(
        info2.get("IPAddress"),
        data.get("IPAddress"),
        data.get("ip"),
    )
    hostname = first_present(
        info2.get("Hostname"),
        data.get("Hostname"),
        data.get("mac"),
    )
    return device, ip, hostname

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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ui_preferences (
            preference_key TEXT PRIMARY KEY,
            preference_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    print(f"[DB] Database initialized: {DATABASE_NAME}")


def get_db_connection():
    conn = sqlite3.connect(DATABASE_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def get_ui_preference(preference_key, default_value):
    initialize_database()
    query = "SELECT preference_value FROM ui_preferences WHERE preference_key = ?"

    with get_db_connection() as conn:
        row = conn.execute(query, (preference_key,)).fetchone()

    if row is None:
        return default_value

    return row["preference_value"]


def set_ui_preference(preference_key, preference_value):
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    query = """
        INSERT INTO ui_preferences (preference_key, preference_value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(preference_key) DO UPDATE SET
            preference_value = excluded.preference_value,
            updated_at = excluded.updated_at
    """

    with sqlite3.connect(DATABASE_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(query, (preference_key, preference_value, timestamp))
        conn.commit()

def write_to_db(device, ip, hostname):
    """Connects to the database and inserts the new record."""
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            cursor = conn.cursor()
            existing = cursor.execute(
                "SELECT id FROM device_details WHERE device_name = ? LIMIT 1",
                (device,)
            ).fetchone()
            is_new = existing is None
            cursor.execute(
                "INSERT INTO device_details (device_name, ip_address, hostname, timestamp) VALUES (?, ?, ?, ?)",
                (device, ip, hostname, timestamp)
            )
            conn.commit()
        label = "[NEW]" if is_new else "[UPDATE]"
        print(f"[DB WRITE] {label} Device '{device}' | IP={ip} | Host={hostname} | Time={timestamp}")
    except sqlite3.Error as e:
        print(f"[ERROR] Database error occurred during write: {e}")


def fetch_latest_devices():
    initialize_database()
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


def delete_device_history(device_name):
    with sqlite3.connect(DATABASE_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM device_details WHERE device_name = ?", (device_name,))
        conn.commit()
        return cursor.rowcount


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


@app.get("/devices.json")
def devices_json():
    return list_devices()


@app.get("/devices-table.json")
def devices_table_json():
    return jsonify(fetch_latest_devices())


@app.get("/api/preferences")
def get_preferences():
    theme = get_ui_preference("theme", "dark")
    return jsonify({"theme": theme})


@app.put("/api/preferences/theme")
def update_theme_preference():
    payload = request.get_json(silent=True) or {}
    theme = payload.get("theme")

    if theme not in {"dark", "light"}:
        return jsonify({"error": "Theme must be 'dark' or 'light'"}), 400

    set_ui_preference("theme", theme)
    return jsonify({"theme": theme})


@app.delete("/api/devices/<path:device_name>")
def delete_device(device_name):
    deleted_rows = delete_device_history(device_name)

    if deleted_rows == 0:
        return jsonify({"error": "Device not found"}), 404

    return jsonify(
        {
            "deleted_device": device_name,
            "deleted_rows": deleted_rows,
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
    
    print(f"[MQTT] Message received | Topic: {topic} | Device: {device} | Payload: {payload[:100]}{'...' if len(payload) > 100 else ''}")
    
    ip = "N/A"
    hostname = "N/A"
    
    try:
        data = json.loads(payload)
        
        if topic.startswith("stat/"):
            device, ip, hostname = parse_stat_init_payload(data, device)
            
        elif topic.startswith("tele/"):
            device, ip, hostname = parse_tele_info2_payload(data, device)

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
            print(f"[WARN] No specific parser found for topic: {topic}")
            return
            
        # Write to DB only if we successfully extracted device name (handled by get_device_id_from_topic)
        write_to_db(device, ip, hostname)
            
    except json.JSONDecodeError:
        print(f"Skipping payload: Could not decode JSON for topic {topic}.")
    except Exception as e:
        print(f"An critical error occurred processing message: {e}")


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        reconnect_attempts = userdata.get("reconnect_attempts", 0)
        if userdata.get("has_connected", False):
            print(
                f"[MQTT] Reconnected successfully after {reconnect_attempts} failed attempt(s). "
                "Ensuring topic subscriptions are active..."
            )
        else:
            print("[MQTT] Connected to broker. Ensuring topic subscriptions are active...")

        userdata["has_connected"] = True
        userdata["reconnect_attempts"] = 0
        for topic in TOPICS:
            client.subscribe(topic)
            print(f"[MQTT] Subscribed to topic: {topic}")
    else:
        print(f"[MQTT] Connect failed with reason code: {reason_code}")


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    if reason_code == 0:
        print("[MQTT] Disconnected cleanly.")
    else:
        print(
            f"[MQTT] Unexpected disconnect (reason code: {reason_code}). "
            "Auto-reconnect is active (backoff 1s to 30s)."
        )


def on_connect_fail(client, userdata):
    reconnect_attempts = userdata.get("reconnect_attempts", 0) + 1
    userdata["reconnect_attempts"] = reconnect_attempts
    print(
        f"[MQTT] Reconnect attempt {reconnect_attempts} failed. "
        "Will retry with exponential backoff."
    )


def run_mqtt_listener():
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="python_device_logger",
        userdata={"has_connected": False, "reconnect_attempts": 0},
    )
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    client.on_connect = on_connect
    client.on_connect_fail = on_connect_fail
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    
    print(f"[MQTT] Attempting to connect to broker at {MQTT_BROKER}:{MQTT_PORT}...")
    print("[MQTT] Reconnect strategy: retry first connection, backoff 1s to 30s.")
    
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, MQTT_KEEPALIVE)
        client.loop_forever(retry_first_connection=True)

    except ConnectionRefusedError:
        print("[ERROR] Connection refused. Ensure the MQTT broker is running and accessible.")
    except Exception as e:
        print(f"[ERROR] An unexpected error occurred: {e}")


def main():
    initialize_database()

    print(f"[CONFIG] MQTT Broker : {MQTT_BROKER}:{MQTT_PORT}")
    print(f"[CONFIG] Keepalive   : {MQTT_KEEPALIVE}s")
    print(f"[CONFIG] Database    : {DATABASE_NAME}")
    print(f"[CONFIG] Listening on {len(TOPICS)} topic(s):")
    for topic in TOPICS:
        print(f"[CONFIG]   {topic}")

    mqtt_thread = threading.Thread(target=run_mqtt_listener, name="mqtt-listener", daemon=True)
    mqtt_thread.start()

    print(f"[WEB] Starting web UI on {WEB_HOST}:{WEB_PORT}")
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
