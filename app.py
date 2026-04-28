import paho.mqtt.client as mqtt
import sqlite3
import json
import datetime
import os
import threading
import queue

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


def to_bool(value, default=False):
    if value is None:
        return default

    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False

    return default


MQTT_BROKER = os.getenv("MQTT_BROKER", "mqtt-broker.local")
MQTT_PORT = get_env_int("MQTT_PORT", 1883)
MQTT_KEEPALIVE = get_env_int("MQTT_KEEPALIVE", 60)
MQTT_QUEUE_MAXSIZE = get_env_int("MQTT_QUEUE_MAXSIZE", 1000)
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
message_queue = queue.Queue(maxsize=MQTT_QUEUE_MAXSIZE)


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
            is_stale INTEGER NOT NULL DEFAULT 0,
            timestamp TEXT NOT NULL
        )
    """)
    ensure_device_details_schema(cursor)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_device_details_active_ip ON device_details (is_stale, ip_address, id)"
    )
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


def ensure_device_details_schema(cursor):
    """Apply minimal forward-compatible schema migrations for device_details."""
    columns = {
        row[1] for row in cursor.execute("PRAGMA table_info(device_details)").fetchall()
    }

    if "is_stale" not in columns:
        cursor.execute(
            "ALTER TABLE device_details ADD COLUMN is_stale INTEGER NOT NULL DEFAULT 0"
        )


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


def has_trackable_ip(ip):
    normalized_ip = first_present(ip).upper()
    return normalized_ip not in {"N/A", "N/A (WLED STATE)", "UNKNOWN"}

def write_to_db(device, ip, hostname):
    """Persist a message while keeping one active row per IP and per device name."""
    if not has_trackable_ip(ip):
        print(f"[DB SKIP] Ignoring message with non-trackable IP for device '{device}'.")
        return

    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            cursor = conn.cursor()
            existing = cursor.execute(
                """
                SELECT id, device_name
                FROM device_details
                WHERE ip_address = ? AND is_stale = 0
                ORDER BY id DESC
                LIMIT 1
                """,
                (ip,)
            ).fetchone()
            is_new = existing is None
            previous_device = existing[1] if existing else None

            # Mark prior active rows stale when either the IP or device name is reused.
            cursor.execute(
                """
                UPDATE device_details
                SET is_stale = 1
                WHERE is_stale = 0
                  AND (ip_address = ? OR device_name = ?)
                """,
                (ip, device)
            )

            cursor.execute(
                """
                INSERT INTO device_details (device_name, ip_address, hostname, is_stale, timestamp)
                VALUES (?, ?, ?, 0, ?)
                """,
                (device, ip, hostname, timestamp)
            )
            conn.commit()

        if is_new:
            label = "[NEW]"
        elif previous_device != device:
            label = "[IP REASSIGNED]"
        else:
            label = "[UPDATE]"

        print(
            f"[DB WRITE] {label} Device '{device}' | IP={ip} | Host={hostname} | Time={timestamp}"
        )

        if previous_device and previous_device != device:
            print(
                f"[DB STALE] IP {ip} moved from '{previous_device}' to '{device}'. "
                "Previous mapping marked stale."
            )
    except sqlite3.Error as e:
        print(f"[ERROR] Database error occurred during write: {e}")


def fetch_latest_devices(include_stale=False):
    initialize_database()
    if include_stale:
        query = """
            SELECT id, device_name, ip_address, hostname, is_stale, timestamp
            FROM device_details
            ORDER BY timestamp DESC, id DESC
        """
    else:
        query = """
            SELECT id, device_name, ip_address, hostname, is_stale, timestamp
            FROM (
                SELECT
                    id,
                    device_name,
                    ip_address,
                    hostname,
                    is_stale,
                    timestamp,
                    ROW_NUMBER() OVER (
                        PARTITION BY LOWER(TRIM(device_name))
                        ORDER BY timestamp DESC, id DESC
                    ) AS row_number
                FROM device_details
                WHERE is_stale = 0
            ) latest
            WHERE row_number = 1
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
    include_stale = to_bool(request.args.get("include_stale"), default=False)
    devices = fetch_latest_devices(include_stale=include_stale)
    return jsonify(
        {
            "devices": devices,
            "count": len(devices),
            "include_stale": include_stale,
            "generated_at": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
    )


@app.get("/devices.json")
def devices_json():
    return list_devices()


@app.get("/devices-table.json")
def devices_table_json():
    include_stale = to_bool(request.args.get("include_stale"), default=False)
    return jsonify(fetch_latest_devices(include_stale=include_stale))


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

def process_incoming_message(topic, payload):
    """Parse and persist an MQTT message outside the network loop thread."""
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


def on_message(client, userdata, msg):
    """Keep callback fast so ping/keepalive handling is not delayed."""
    topic = msg.topic
    payload = msg.payload.decode("utf-8", errors="replace")

    try:
        message_queue.put_nowait((topic, payload))
    except queue.Full:
        print(
            "[MQTT] Message queue is full; dropping message to preserve connection health. "
            f"Topic: {topic}"
        )


def message_processor_loop():
    while True:
        topic, payload = message_queue.get()
        try:
            process_incoming_message(topic, payload)
        finally:
            message_queue.task_done()


def is_session_present(flags):
    if flags is None:
        return False

    if hasattr(flags, "session_present"):
        return bool(flags.session_present)

    if isinstance(flags, dict):
        return bool(flags.get("session present") or flags.get("session_present"))

    return False


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        reconnect_attempts = userdata.get("reconnect_attempts", 0)
        session_present = is_session_present(flags)

        if userdata.get("has_connected", False):
            print(
                f"[MQTT] Reconnected successfully after {reconnect_attempts} failed attempt(s). "
                f"Session present={session_present}."
            )
        else:
            print(f"[MQTT] Connected to broker. Session present={session_present}.")

        userdata["has_connected"] = True
        userdata["reconnect_attempts"] = 0

        if session_present:
            print("[MQTT] Broker kept the existing session; skipping resubscribe.")
        else:
            print("[MQTT] Subscribing to topics...")
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
        clean_session=False,
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
    print(f"[CONFIG] Queue size  : {MQTT_QUEUE_MAXSIZE}")
    print(f"[CONFIG] Database    : {DATABASE_NAME}")
    print(f"[CONFIG] Listening on {len(TOPICS)} topic(s):")
    for topic in TOPICS:
        print(f"[CONFIG]   {topic}")

    worker_thread = threading.Thread(target=message_processor_loop, name="mqtt-message-processor", daemon=True)
    worker_thread.start()

    mqtt_thread = threading.Thread(target=run_mqtt_listener, name="mqtt-listener", daemon=True)
    mqtt_thread.start()

    print(f"[WEB] Starting web UI on {WEB_HOST}:{WEB_PORT}")
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
