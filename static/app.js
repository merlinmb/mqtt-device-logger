const state = {
  allDevices: [],
  visibleDevices: [],
  sortKey: "timestamp",
  sortDirection: "desc",
  query: "",
};

const searchInput = document.querySelector("#search-input");
const refreshButton = document.querySelector("#refresh-button");
const statusPill = document.querySelector("#status-pill");
const tableBody = document.querySelector("#device-table-body");
const visibleCount = document.querySelector("#visible-count");
const totalCount = document.querySelector("#total-count");
const generatedAt = document.querySelector("#generated-at");
const resultsCopy = document.querySelector("#results-copy");
const sortButtons = Array.from(document.querySelectorAll(".sort-button"));

function normalizeValue(value) {
  return (value ?? "").toString().trim();
}

function compareValues(left, right) {
  return left.localeCompare(right, undefined, { numeric: true, sensitivity: "base" });
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
        <tr>
          <td class="device-name">${escapeHtml(device.device_name)}</td>
          <td class="ip">${escapeHtml(device.ip_address)}</td>
          <td class="hostname">${escapeHtml(device.hostname || "-")}</td>
          <td class="timestamp">${escapeHtml(device.timestamp)}</td>
        </tr>
      `,
    )
    .join("");
}

function updateSummary() {
  visibleCount.textContent = state.visibleDevices.length.toString();
  totalCount.textContent = state.allDevices.length.toString();
  resultsCopy.textContent = state.query
    ? `Showing ${state.visibleDevices.length} fuzzy matches for "${state.query}".`
    : `Showing ${state.visibleDevices.length} latest device rows.`;
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

searchInput.addEventListener("input", (event) => {
  state.query = event.target.value;
  applyFilters();
});

refreshButton.addEventListener("click", () => {
  loadDevices();
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

updateSortIndicators();
loadDevices();
window.setInterval(loadDevices, 15000);