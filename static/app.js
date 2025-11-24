const REFRESH_INTERVAL_MS = 30000;

const els = {
  lastSeverity: document.getElementById("last-severity"),
  lastRefresh: document.getElementById("last-refresh"),
  podsTableBody: document.getElementById("pods-table-body"),
  cpuPercent: document.getElementById("cpu-percent"),
  cpuFill: document.getElementById("cpu-fill"),
  cpuDetail: document.getElementById("cpu-detail"),
  memoryPercent: document.getElementById("memory-percent"),
  memoryFill: document.getElementById("memory-fill"),
  memoryDetail: document.getElementById("memory-detail"),
  storagePercent: document.getElementById("storage-percent"),
  storageFill: document.getElementById("storage-fill"),
  storageDetail: document.getElementById("storage-detail"),
  podsPercent: document.getElementById("pods-percent"),
  podsFill: document.getElementById("pods-fill"),
  podsDetail: document.getElementById("pods-detail"),
  nodesPercent: document.getElementById("nodes-percent"),
  nodesFill: document.getElementById("nodes-fill"),
  nodesDetail: document.getElementById("nodes-detail"),
  alertIncidents: document.getElementById("alert-incidents"),
  alertQueues: document.getElementById("alert-queues"),
  nsTopCpu: document.getElementById("ns-top-cpu"),
  nsTopMemory: document.getElementById("ns-top-memory"),
  nsUnhealthy: document.getElementById("ns-unhealthy"),
  podsTotalPill: document.getElementById("pods-total-pill"),
  errorBanner: document.getElementById("cluster-errors"),
  errorList: document.getElementById("error-list"),
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

function gaugeTone(percent = 0) {
  if (percent > 90) return "tone-red";
  if (percent > 80) return "tone-orange";
  if (percent > 60) return "tone-yellow";
  return "tone-green";
}

function setGauge({ fill, value, text }, percent = 0, detail = "") {
  const safePercent = Number.isFinite(percent) ? Math.max(0, Math.min(100, percent)) : 0;
  const tone = gaugeTone(safePercent);
  if (fill) {
    fill.style.width = `${safePercent}%`;
    fill.classList.remove("tone-green", "tone-yellow", "tone-orange", "tone-red");
    fill.classList.add(tone);
  }
  if (value) {
    value.textContent = `${safePercent.toFixed(1)}%`;
  }
  if (text) {
    text.textContent = detail;
  }
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

function renderNamespaceRows(target, rows, emptyText = "No data") {
  if (!target) return;
  target.innerHTML = "";
  const columnCount = target.closest("table")?.querySelectorAll("th").length || 3;
  if (!Array.isArray(rows) || rows.length === 0) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="${columnCount}" class="muted">${emptyText}</td>`;
    target.appendChild(tr);
    return;
  }

  rows.forEach((row) => {
    const tr = document.createElement("tr");
    const values = Object.values(row).map((val) => (val ?? "").toString());
    tr.innerHTML = values.map((val) => `<td>${val}</td>`).join("");
    target.appendChild(tr);
  });
}

async function loadClusterPods() {
  try {
    const resp = await fetch("/api/cluster/pods");
    if (!resp.ok) throw new Error(`Failed to load pods: ${resp.status}`);
    const data = await resp.json();
    const pods = Array.isArray(data.pods) ? data.pods : [];
    const summary = data.summary || {};
    const nodes = summary.nodes || {};
    const cpu = summary.cpu || {};
    const memory = summary.memory || {};
    const storage = summary.storage || {};
    const podsSummary = summary.pods || {};
    const alerts = summary.alerts || {};
    const queues = summary.queues || {};

    if (els.lastRefresh) {
      els.lastRefresh.textContent = `Updated ${new Date().toLocaleTimeString()}`;
    }

    setGauge(
      { fill: els.cpuFill, value: els.cpuPercent, text: els.cpuDetail },
      Number(cpu.utilization_percent ?? 0),
      `${(cpu.used_cores ?? 0).toFixed(2)} / ${(cpu.total_cores ?? 0).toFixed(2)} cores`,
    );

    setGauge(
      { fill: els.memoryFill, value: els.memoryPercent, text: els.memoryDetail },
      Number(memory.utilization_percent ?? 0),
      `${(memory.used_gib ?? 0).toFixed(2)} / ${(memory.total_gib ?? 0).toFixed(2)} GiB`,
    );

    setGauge(
      { fill: els.storageFill, value: els.storagePercent, text: els.storageDetail },
      Number(storage.utilization_percent ?? 0),
      `${(storage.used_gib ?? 0).toFixed(2)} / ${(storage.total_gib ?? 0).toFixed(2)} GiB`,
    );

    const total = podsSummary.total ?? pods.length;
    const bad = podsSummary.unhealthy ?? 0;
    const badPercent = total > 0 ? (bad / total) * 100 : 0;
    setGauge(
      { fill: els.podsFill, value: els.podsPercent, text: els.podsDetail },
      podsSummary.unhealthy_percent ?? badPercent,
      `${bad} unhealthy of ${total} pods`,
    );

    const notReady = nodes.not_ready ?? 0;
    const ready = nodes.ready ?? 0;
    const nodeTotal = nodes.count ?? ready + notReady;
    const notReadyPercent = nodeTotal > 0 ? (notReady / nodeTotal) * 100 : 0;
    setGauge(
      { fill: els.nodesFill, value: els.nodesPercent, text: els.nodesDetail },
      notReadyPercent,
      `${ready} ready / ${notReady} not-ready`,
    );

    if (els.lastSeverity) {
      const severity = alerts.last_severity || "—";
      els.lastSeverity.textContent = severity === "—" ? "—" : severity.toString().toUpperCase();
    }
    if (els.alertIncidents) {
      const incidents = alerts.open_incidents ?? 0;
      els.alertIncidents.textContent = `Open incidents: ${incidents}`;
    }
    if (els.alertQueues) {
      const enabled = queues.enabled === true;
      const totalBacklog = queues.total_backlog ?? 0;
      els.alertQueues.textContent = enabled
        ? `Queues backlog: ${totalBacklog}`
        : "Queues disabled";
    }

    if (els.podsTotalPill) {
      els.podsTotalPill.textContent = `${total} pods`;
    }

    renderNamespaceRows(
      els.nsTopCpu,
      (data.namespaces?.top_by_cpu || []).map((item) => ({
        Namespace: item.namespace || "",
        "CPU (m)": item.cpu_mcores ?? 0,
        Pods: item.pods ?? 0,
      })),
    );

    renderNamespaceRows(
      els.nsTopMemory,
      (data.namespaces?.top_by_memory || []).map((item) => ({
        Namespace: item.namespace || "",
        "Memory (Mi)": item.memory_mib ?? 0,
        Pods: item.pods ?? 0,
      })),
    );

    renderNamespaceRows(
      els.nsUnhealthy,
      (data.namespaces?.unhealthy_counts || []).map((item) => ({
        Namespace: item.namespace || "",
        Unhealthy: item.unhealthy_pods ?? 0,
      })),
      "All namespaces healthy",
    );

    els.podsTableBody.innerHTML = "";
    pods.forEach((pod) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${pod.namespace || ""}</td>
        <td>${pod.name || ""}</td>
        <td>${pod.status || ""}</td>
        <td>${pod.restarts ?? ""}</td>
        <td>${pod.age || ""}</td>
        <td>${pod.node || ""}</td>
      `;
      els.podsTableBody.appendChild(tr);
    });

    const errorList = Array.isArray(data.errors) ? data.errors : [];
    if (els.errorBanner && els.errorList) {
      els.errorList.innerHTML = "";
      if (errorList.length) {
        els.errorBanner.hidden = false;
        errorList.forEach((err) => {
          const li = document.createElement("li");
          li.textContent = err;
          els.errorList.appendChild(li);
        });
      } else {
        els.errorBanner.hidden = true;
      }
    }
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
