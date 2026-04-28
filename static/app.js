const state = {
  allDevices: [],
  visibleDevices: [],
  sortKey: "timestamp",
  sortDirection: "desc",
  query: "",
  theme: "dark",
};

const body = document.body;
const searchInput = document.querySelector("#search-input");
const refreshButton = document.querySelector("#refresh-button");
const statusPill = document.querySelector("#status-pill");
const tableBody = document.querySelector("#device-table-body");
const visibleCount = document.querySelector("#visible-count");
const totalCount = document.querySelector("#total-count");
const generatedAt = document.querySelector("#generated-at");
const resultsCopy = document.querySelector("#results-copy");
const sortButtons = Array.from(document.querySelectorAll(".sort-button"));
const themeToggle = document.querySelector("#theme-toggle");
const themeToggleText = document.querySelector("#theme-toggle-text");
const jsonLink = document.querySelector("#json-link");
const copyJsonLinkButton = document.querySelector("#copy-json-link");

function getJsonLink() {
  return new URL("/devices.json", window.location.href).toString();
}

function syncJsonLink() {
  if (jsonLink) {
    jsonLink.href = getJsonLink();
  }
}

function deviceDeleteLabel(deviceName) {
  return `Delete ${normalizeValue(deviceName) || "device"}`;
}

function deviceIpHref(ipAddress) {
  const normalizedIp = normalizeValue(ipAddress);

  if (!normalizedIp) {
    return "";
  }

  return /^[a-z]+:\/\//i.test(normalizedIp) ? normalizedIp : `http://${normalizedIp}`;
}

function hasNavigableIp(ipAddress) {
  const normalizedIp = normalizeValue(ipAddress);
  return normalizedIp && normalizedIp.toUpperCase() !== "N/A";
}

function normalizeValue(value) {
  return (value ?? "").toString().trim();
}

function compareValues(left, right) {
  return left.localeCompare(right, undefined, { numeric: true, sensitivity: "base" });
}

function applyTheme(theme) {
  const nextTheme = theme === "light" ? "light" : "dark";
  state.theme = nextTheme;
  body.dataset.theme = nextTheme;
  themeToggle.setAttribute("aria-pressed", String(nextTheme === "dark"));
  themeToggleText.textContent = nextTheme === "dark" ? "Dark" : "Light";
}

function fuzzyScore(haystack, needle) {
  if (!needle) {
    return 1;
  }

  const source = haystack.toLowerCase();
  const query = needle.toLowerCase();

  if (source.includes(query)) {
    return 1000 - source.indexOf(query);
  }

  let score = 0;
  let queryIndex = 0;
  let consecutive = 0;

  for (let sourceIndex = 0; sourceIndex < source.length && queryIndex < query.length; sourceIndex += 1) {
    if (source[sourceIndex] !== query[queryIndex]) {
      consecutive = 0;
      continue;
    }

    consecutive += 1;
    score += 8 + consecutive * 4;
    queryIndex += 1;
  }

  return queryIndex === query.length ? score : 0;
}

function applyFilters() {
  const query = state.query.trim().toLowerCase();

  const filtered = state.allDevices
    .map((device) => {
      const searchBlob = [
        device.device_name,
        device.ip_address,
        device.hostname,
        device.timestamp,
      ]
        .map(normalizeValue)
        .join(" ");

      return {
        ...device,
        _score: fuzzyScore(searchBlob, query),
      };
    })
    .filter((device) => device._score > 0 || !query);

  filtered.sort((left, right) => {
    const leftValue = normalizeValue(left[state.sortKey]);
    const rightValue = normalizeValue(right[state.sortKey]);

    if (query && left._score !== right._score) {
      return right._score - left._score;
    }

    const comparison = compareValues(leftValue, rightValue);
    return state.sortDirection === "asc" ? comparison : -comparison;
  });

  state.visibleDevices = filtered;
  renderTable();
  updateSummary();
}

function renderTable() {
  if (!state.visibleDevices.length) {
    tableBody.innerHTML = '<tr><td colspan="4" class="empty-state">No devices match the current search.</td></tr>';
    return;
  }

  tableBody.innerHTML = state.visibleDevices
    .map(
      (device) => `
        <tr data-device-name="${escapeHtml(device.device_name)}">
          <td class="device-name-cell">
            <span class="device-name">${escapeHtml(device.device_name)}</span>
            <button
              type="button"
              class="row-delete-button"
              data-device-name="${escapeHtml(device.device_name)}"
              aria-label="${escapeHtml(deviceDeleteLabel(device.device_name))}"
              title="${escapeHtml(deviceDeleteLabel(device.device_name))}"
            >
              ×
            </button>
          </td>
          <td class="ip">${hasNavigableIp(device.ip_address)
            ? `<a href="${escapeHtml(deviceIpHref(device.ip_address))}" target="_blank" rel="noreferrer">${escapeHtml(device.ip_address)}</a>`
            : escapeHtml(device.ip_address || "-")}</td>
          <td class="hostname">${escapeHtml(device.hostname || "-")}</td>
          <td class="timestamp">${escapeHtml(device.timestamp)}</td>
        </tr>
      `,
    )
    .join("");
}

async function deleteDevice(deviceName) {
  const normalizedName = normalizeValue(deviceName);

  if (!normalizedName) {
    return;
  }

  state.allDevices = state.allDevices.filter((device) => normalizeValue(device.device_name) !== normalizedName);
  applyFilters();
  statusPill.textContent = `Deleting ${normalizedName}`;

  try {
    const response = await fetch(`/api/devices/${encodeURIComponent(normalizedName)}`, {
      method: "DELETE",
      headers: { Accept: "application/json" },
    });

    if (!response.ok) {
      throw new Error(`Delete failed with ${response.status}`);
    }

    statusPill.textContent = `Deleted ${normalizedName}`;
  } catch (error) {
    statusPill.textContent = "Delete failed";
    resultsCopy.textContent = error.message;
    await loadDevices();
  }
}

function updateSummary() {
  if (visibleCount) {
    visibleCount.textContent = state.visibleDevices.length.toString();
  }

  if (totalCount) {
    totalCount.textContent = state.allDevices.length.toString();
  }

  resultsCopy.textContent = state.query
    ? `Showing ${state.visibleDevices.length} fuzzy matches for "${state.query}".`
    : `Showing ${state.visibleDevices.length} active IP-owned rows.`;
}

function updateSortIndicators() {
  sortButtons.forEach((button) => {
    if (button.dataset.sortKey === state.sortKey) {
      button.dataset.direction = state.sortDirection;
      return;
    }

    button.dataset.direction = "";
  });
}

function escapeHtml(value) {
  return normalizeValue(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function loadDevices() {
  statusPill.textContent = "Refreshing";

  try {
    const response = await fetch("/api/devices", { headers: { Accept: "application/json" } });

    if (!response.ok) {
      throw new Error(`Request failed with ${response.status}`);
    }

    const payload = await response.json();
    state.allDevices = payload.devices ?? [];
    generatedAt.textContent = payload.generated_at ?? "-";
    statusPill.textContent = `Synced ${state.allDevices.length} devices`;
    applyFilters();
  } catch (error) {
    statusPill.textContent = "Refresh failed";
    resultsCopy.textContent = error.message;

    if (!state.allDevices.length) {
      tableBody.innerHTML = '<tr><td colspan="4" class="empty-state">Unable to load device data.</td></tr>';
    }
  }
}

async function loadPreferences() {
  try {
    const response = await fetch("/api/preferences", { headers: { Accept: "application/json" } });

    if (!response.ok) {
      throw new Error(`Preference request failed with ${response.status}`);
    }

    const payload = await response.json();
    applyTheme(payload.theme);
  } catch {
    applyTheme("dark");
  }
}

async function persistTheme(theme) {
  const response = await fetch("/api/preferences/theme", {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify({ theme }),
  });

  if (!response.ok) {
    throw new Error(`Theme update failed with ${response.status}`);
  }

  return response.json();
}

async function copyJsonLink() {
  const jsonUrl = getJsonLink();

  try {
    await navigator.clipboard.writeText(jsonUrl);
    statusPill.textContent = "JSON link copied";
  } catch {
    resultsCopy.textContent = jsonUrl;
    statusPill.textContent = "Copy failed";
  }
}

searchInput.addEventListener("input", (event) => {
  state.query = event.target.value;
  applyFilters();
});

refreshButton.addEventListener("click", () => {
  loadDevices();
});

copyJsonLinkButton?.addEventListener("click", () => {
  copyJsonLink();
});

themeToggle.addEventListener("click", async () => {
  const previousTheme = state.theme;
  const nextTheme = previousTheme === "dark" ? "light" : "dark";

  applyTheme(nextTheme);

  try {
    await persistTheme(nextTheme);
    statusPill.textContent = `${nextTheme === "dark" ? "Dark" : "Light"} mode saved`;
  } catch (error) {
    applyTheme(previousTheme);
    statusPill.textContent = "Theme save failed";
    resultsCopy.textContent = error.message;
  }
});

tableBody.addEventListener("click", (event) => {
  const deleteButton = event.target.closest(".row-delete-button");

  if (!deleteButton) {
    return;
  }

  deleteDevice(deleteButton.dataset.deviceName);
});

sortButtons.forEach((button) => {
  button.addEventListener("click", () => {
    const nextKey = button.dataset.sortKey;

    if (state.sortKey === nextKey) {
      state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";
    } else {
      state.sortKey = nextKey;
      state.sortDirection = "asc";
    }

    updateSortIndicators();
    applyFilters();
  });
});

syncJsonLink();
updateSortIndicators();
applyTheme("dark");
loadPreferences();
loadDevices();
window.setInterval(loadDevices, 15000);