# MQTT Device Logger

Small Python service that subscribes to MQTT topics, extracts device identity and network metadata from incoming JSON payloads, and stores the results in a local SQLite database.

It now also serves a browser UI for exploring the latest device records with fuzzy search and sortable table columns.

## What It Does

- Connects to an MQTT broker with `paho-mqtt`
- Subscribes to a fixed set of device-related topics
- Parses supported payload shapes for IP address and hostname data
- Writes each observed device update to `device_data.db`
- Serves a frontend with quick filtering, sort controls, and live refresh
- Treats IP address as the active identity and marks replaced IP mappings as stale

The current implementation listens to these topics:

- `stat/+/init`
- `tele/+/INFO2`
- `wled/+/state`
- `home/device/+/info`

Each matching message is written to the `device_details` table with:

- `device_name`
- `ip_address`
- `hostname`
- `is_stale`
- `timestamp`

When a new message arrives for an IP already owned by another active row, the previous active row for that IP is marked stale and the newest row becomes the active mapping.

## Project Files

- `app.py`: MQTT listener and SQLite writer
- `requirements.txt`: Python dependency list
- `Dockerfile.txt`: Container image definition
- `deploy.ps1`: PowerShell deployment helper for a remote Docker host over SSH
- `.env.example`: Safe example environment configuration for local runs and deployment

## Requirements

- Python 3.9+
- Access to an MQTT broker
- Network access to the broker from the machine or container running the app

## Local Setup

Create and activate a virtual environment, then install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a local environment file from the example and update it for your broker:

```powershell
Copy-Item .env.example .env
```

Run the service:

```powershell
python app.py
```

On startup the app creates `device_data.db` in the project directory if it does not already exist, starts the MQTT listener in a background thread, and serves the web UI at `http://localhost:8000`.

## Web UI

The frontend is available at:

```text
http://localhost:8000
```

Features:

- quick fuzzy search across device name, host, IP, and timestamp
- sortable table headers for each visible column
- summary cards showing device totals and refresh time
- manual refresh plus automatic polling of the latest rows

The browser reads data from the JSON endpoint below:

```text
GET /api/devices
```

Optional query parameter:

- `include_stale=true` to include historical stale rows alongside active rows

Examples:

```text
GET /api/devices?include_stale=true
GET /devices-table.json?include_stale=true
```

## Configuration

Configuration is read from environment variables. The repository includes `.env.example` as a safe template.

The default keys are:

```python
MQTT_BROKER = "mqtt-broker.local"
MQTT_PORT = 1883
MQTT_KEEPALIVE = 60
DATABASE_NAME = "device_data.db"
WEB_HOST = "0.0.0.0"
WEB_PORT = 8000
```

Subscribed topics can be overridden with `MQTT_TOPICS` as a comma-separated value.

If you need to point the service at a different broker or topic structure, update `.env` before running or building the container.

## Supported Payload Handling

The service currently expects JSON payloads and applies topic-specific parsing:

- `stat/+/init`: reads `ip` and `hostname`
- `tele/+/INFO2`: reads `Info2.IPAddress` and `Info2.Hostname`
- `wled/+/state`: reads `ip` when present and assigns a generic hostname
- `home/device/+/info`: reads top-level `ip` and `hostname`

If a payload is not valid JSON, the message is skipped.

## Database

The app stores records in SQLite using this schema:

```sql
CREATE TABLE IF NOT EXISTS device_details (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_name TEXT NOT NULL,
    ip_address TEXT NOT NULL,
    hostname TEXT,
    timestamp TEXT NOT NULL
)
```

You can inspect the database with any SQLite client, for example:

```powershell
sqlite3 device_data.db "SELECT * FROM device_details ORDER BY id DESC LIMIT 20;"
```

## Docker

The repository includes `Dockerfile.txt` rather than the default `Dockerfile` name, so build commands need `-f`.

Build the image:

```powershell
docker build -f Dockerfile.txt -t mqtt-device-logger .
```

Run the container:

```powershell
docker run --rm --name mqtt-device-logger --env-file .env mqtt-device-logger
```

If the container needs access to services on your local network, ensure Docker networking allows the broker address from `.env` to be reached.

## Remote Deployment

`deploy.ps1` is intended to deploy the app to a remote Docker host over SSH. It performs these high-level steps:

- verifies local `ssh` and `scp`
- checks remote `docker` and `git` availability
- clones or updates the repository on the remote host
- creates a remote `.env` file if missing
- rebuilds and starts the container with `docker build` and `docker run`
- performs a health check and shows recent logs

If you do not pass `-Port`, the script now publishes the same host port as `WEB_PORT` in the remote `.env`. That keeps direct browser access and reverse proxies such as nginx aligned with the app's configured listen port.

Run it with defaults:

```powershell
.\deploy.ps1
```

Example with explicit target and rebuild:

```powershell
.\deploy.ps1 -TargetHost mqtt-host.local -User deploy -Rebuild
```

## Notes And Limitations

- The UI shows active (non-stale) rows keyed by IP, not a full historical browse/filter view.
- Payload parsing is intentionally narrow and only covers the topic shapes implemented in `app.py`.

## Next Improvements

- add structured logging
- add tests around topic parsing
- expose recent device observations through a small API or export command