const REFRESH_INTERVAL_MS = 30000;

const els = {
  totalPods: document.getElementById("total-pods"),
  badPods: document.getElementById("bad-pods"),
  lastSeverity: document.getElementById("last-severity"),
  podsTableBody: document.getElementById("pods-table-body"),
  nightStatusText: document.getElementById("night-status-text"),
  nightStatusIndicator: document.getElementById("night-status-indicator"),
  nightEventsLog: document.getElementById("night-events-log"),
  nightReportSelect: document.getElementById("night-report-select"),
  nightReportViewer: document.getElementById("night-report-viewer"),
  nightReportButton: document.getElementById("btn-view-report"),
  btnNightStart: document.getElementById("btn-night-start"),
  btnNightStop: document.getElementById("btn-night-stop"),
  chatForm: document.getElementById("chat-form"),
  chatMessage: document.getElementById("chat-message"),
  chatSend: document.getElementById("btn-chat-send"),
  chatResponse: document.getElementById("chat-response-text"),
};

function notify(message, type = "info") {
  const toast = document.createElement("div");
  toast.textContent = message;
  toast.style.position = "fixed";
  toast.style.right = "16px";
  toast.style.bottom = "16px";
  toast.style.padding = "10px 12px";
  toast.style.borderRadius = "10px";
  toast.style.background = type === "error" ? "#ef4444" : "#38bdf8";
  toast.style.color = "#0b1220";
  toast.style.boxShadow = "0 10px 24px rgba(0,0,0,0.25)";
  toast.style.zIndex = "1000";
  document.body.appendChild(toast);
  setTimeout(() => {
    toast.style.transition = "opacity 200ms ease";
    toast.style.opacity = "0";
    setTimeout(() => toast.remove(), 220);
  }, 2200);
}

function truncate(text, max = 160) {
  if (!text) return "";
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function firstLine(text = "") {
  const [line] = text.toString().split(/\r?\n/);
  return line ? line.trim() : "";
}

function severityTone(level = "") {
  const normalized = level.toString().toLowerCase();
  if (normalized === "ok" || normalized === "low") return "ok";
  if (normalized === "medium") return "medium";
  if (normalized === "high" || normalized === "critical") return "high";
  return "info";
}

function setStatusIndicator(running) {
  const indicator = els.nightStatusIndicator;
  indicator.classList.remove("running", "stopped", "idle");
  if (running === true) {
    indicator.classList.add("running");
    els.nightStatusText.textContent = "Running";
  } else if (running === false) {
    indicator.classList.add("stopped");
    els.nightStatusText.textContent = "Stopped";
  } else {
    indicator.classList.add("idle");
    els.nightStatusText.textContent = "Unknown";
  }
}

async function loadClusterPods() {
  try {
    const resp = await fetch("/api/cluster/pods");
    if (!resp.ok) throw new Error(`Failed to load pods: ${resp.status}`);
    const data = await resp.json();
    const pods = Array.isArray(data.pods) ? data.pods : [];
    const summary = data.summary || {};

    const total = summary.total_pods ?? pods.length;
    const bad =
      summary.bad_pods ??
      pods.filter(
        (p) => (p.status !== "Running" && p.status !== "Completed") || Number(p.restarts) > 5,
      ).length;

    els.totalPods.textContent = total;
    els.badPods.textContent = bad;

    els.podsTableBody.innerHTML = "";
    pods.forEach((pod) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${pod.namespace || ""}</td>
        <td>${pod.name || ""}</td>
        <td>${pod.status || ""}</td>
        <td>${pod.restarts ?? ""}</td>
      `;
      els.podsTableBody.appendChild(tr);
    });
  } catch (err) {
    console.error(err);
    notify("Failed to load cluster pods", "error");
  }
}

async function loadNightStatus() {
  try {
    const resp = await fetch("/api/night/status");
    if (!resp.ok) throw new Error(`Failed to load status: ${resp.status}`);
    const data = await resp.json();
    setStatusIndicator(Boolean(data.running));
  } catch (err) {
    console.error(err);
    notify("Failed to load night status", "error");
  }
}

function renderEventItem(event) {
  const wrapper = document.createElement("div");
  wrapper.className = "event-item";
  const timestamp = event.timestamp || "";
  const analysis = event.analysis || {};
  const severity = analysis.severity || event.severity || "info";
  const tone = severityTone(severity);
  const title = analysis.title || event.title || "";
  const summary = firstLine(analysis.summary || event.summary || "");

  const meta = document.createElement("div");
  meta.className = "event-meta";

  const severityPill = document.createElement("span");
  severityPill.className = `severity-pill severity-${tone}`;
  severityPill.textContent = severity.toString().toUpperCase();

  const timestampEl = document.createElement("span");
  timestampEl.className = "event-timestamp";
  timestampEl.textContent = timestamp;

  meta.appendChild(severityPill);
  meta.appendChild(timestampEl);

  const titleEl = document.createElement("div");
  titleEl.className = "event-title";
  titleEl.textContent = title;

  const summaryEl = document.createElement("div");
  summaryEl.className = "event-summary";
  summaryEl.textContent = truncate(summary, 200);

  wrapper.appendChild(meta);
  wrapper.appendChild(titleEl);
  wrapper.appendChild(summaryEl);

  return { element: wrapper, severity };
}

async function loadNightEvents() {
  try {
    const resp = await fetch("/api/night/events");
    if (!resp.ok) throw new Error(`Failed to load events: ${resp.status}`);
    const events = await resp.json();
    const list = Array.isArray(events) ? events : [];

    els.nightEventsLog.innerHTML = "";
    let latestSeverity = "—";

    list.forEach((evt, idx) => {
      const { element, severity } = renderEventItem(evt);
      if (idx === 0 && severity) {
        latestSeverity = severity.toString();
      }
      els.nightEventsLog.appendChild(element);
    });

    els.lastSeverity.textContent = latestSeverity === "—" ? "—" : latestSeverity.toUpperCase();
  } catch (err) {
    console.error(err);
    notify("Failed to load night events", "error");
  }
}

async function loadNightReports() {
  try {
    const resp = await fetch("/api/night/reports");
    if (!resp.ok) throw new Error(`Failed to load reports: ${resp.status}`);
    const reports = await resp.json();
    const list = Array.isArray(reports) ? reports : [];

    if (!els.nightReportSelect) return;

    els.nightReportSelect.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = list.length ? "Select a report…" : "No reports available";
    els.nightReportSelect.appendChild(placeholder);

    list.forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      els.nightReportSelect.appendChild(opt);
    });

    if (!list.length && els.nightReportViewer) {
      els.nightReportViewer.textContent = "No reports available yet.";
    }
  } catch (err) {
    console.error(err);
    notify("Failed to load night reports", "error");
  }
}

async function viewSelectedReport() {
  if (!els.nightReportSelect) return;
  const filename = els.nightReportSelect.value;
  if (!filename) {
    notify("Please select a report first", "error");
    return;
  }

  if (els.nightReportViewer) {
    els.nightReportViewer.textContent = "Loading report...";
  }

  try {
    const resp = await fetch(`/api/night/report/${encodeURIComponent(filename)}`);
    if (!resp.ok) throw new Error(`Failed to load report: ${resp.status}`);
    const text = await resp.text();
    if (els.nightReportViewer) {
      els.nightReportViewer.textContent = text || "(No content)";
    }
  } catch (err) {
    console.error(err);
    notify("Failed to load report", "error");
    if (els.nightReportViewer) {
      els.nightReportViewer.textContent = "Unable to load report.";
    }
  }
}

async function postNightAction(endpoint) {
  try {
    const resp = await fetch(endpoint, { method: "POST" });
    if (!resp.ok) {
      const errText = await resp.text();
      throw new Error(errText || `Request failed: ${resp.status}`);
    }
    const data = await resp.json();
    await loadNightStatus();
    if (endpoint.includes("start")) {
      notify("Night Mode started");
      setStatusIndicator(true);
    } else if (endpoint.includes("stop")) {
      notify("Night Mode stopped");
      setStatusIndicator(false);
    }
    return data;
  } catch (err) {
    console.error(err);
    notify("Night action failed", "error");
  }
}

async function sendChatMessage() {
  const message = (els.chatMessage.value || "").trim();
  if (!message) {
    notify("Please enter a message", "error");
    return;
  }

  const endpoint = els.chatForm?.dataset?.endpoint || "/api/chat";
  els.chatResponse.textContent = "Typing…";

  try {
    const resp = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    if (!resp.ok) throw new Error(`Chat failed: ${resp.status}`);
    const data = await resp.json();
    els.chatResponse.textContent = data.answer || "(No response)";
  } catch (err) {
    console.error(err);
    els.chatResponse.textContent = "Failed to get response.";
    notify("Chat request failed", "error");
  }
}

function setupNightButtons() {
  if (els.btnNightStart) {
    els.btnNightStart.addEventListener("click", () => {
      const endpoint = els.btnNightStart.dataset.endpoint;
      postNightAction(endpoint);
    });
  }
  if (els.btnNightStop) {
    els.btnNightStop.addEventListener("click", () => {
      const endpoint = els.btnNightStop.dataset.endpoint;
      postNightAction(endpoint);
    });
  }
}

function setupNightReports() {
  if (els.nightReportButton) {
    els.nightReportButton.addEventListener("click", (ev) => {
      ev.preventDefault();
      viewSelectedReport();
    });
  }
}

function setupChat() {
  if (els.chatSend) {
    els.chatSend.addEventListener("click", (ev) => {
      ev.preventDefault();
      sendChatMessage();
    });
  }
}

function startPolling() {
  setInterval(loadClusterPods, REFRESH_INTERVAL_MS);
  setInterval(loadNightStatus, REFRESH_INTERVAL_MS);
  setInterval(loadNightEvents, REFRESH_INTERVAL_MS);
}

window.addEventListener("DOMContentLoaded", () => {
  loadClusterPods();
  loadNightStatus();
  loadNightEvents();
  loadNightReports();
  setupNightButtons();
  setupNightReports();
  setupChat();
  startPolling();
});
