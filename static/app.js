const REFRESH_INTERVAL_MS = 30000;
const SUMMARY_REFRESH_MS = 150000;
const NIGHT_START_ENDPOINT = "/api/night/start";
const NIGHT_STOP_ENDPOINT = "/api/night/stop";

const els = {
  lastSeverity: document.getElementById("last-severity"),
  metricsUpdated: document.getElementById("metrics-updated-at"),
  overallHealth: document.getElementById("overall-health-pill"),
  podsTableBody: document.getElementById("pods-table-body"),
  podsTableContainer: document.getElementById("pods-table-container"),
  cpuPercent: document.getElementById("cpu-util"),
  cpuFill: document.getElementById("cpu-bar-fill"),
  cpuDetail: document.getElementById("cpu-cores"),
  memoryPercent: document.getElementById("memory-util"),
  memoryFill: document.getElementById("memory-bar-fill"),
  memoryDetail: document.getElementById("memory-usage"),
  storagePercent: document.getElementById("storage-util"),
  storageFill: document.getElementById("storage-bar-fill"),
  storageDetail: document.getElementById("storage-usage"),
  podsPercent: document.getElementById("pods-percent"),
  podsFill: document.getElementById("pods-bar-fill"),
  podsDetail: document.getElementById("pods-detail"),
  nodesPercent: document.getElementById("nodes-percent"),
  nodesFill: document.getElementById("nodes-bar-fill"),
  nodesDetail: document.getElementById("nodes-detail"),
  alertIncidents: document.getElementById("alert-incidents"),
  alertQueues: document.getElementById("alert-queues"),
  nsTopCpu: document.getElementById("ns-top-cpu"),
  nsTopMemory: document.getElementById("ns-top-memory"),
  nsUnhealthy: document.getElementById("ns-unhealthy"),
  podsTotalPill: document.getElementById("pods-total-pill"),
  errorBanner: document.getElementById("cluster-errors"),
  errorList: document.getElementById("error-list"),
  nightStatusPill: document.getElementById("night-status-pill"),
  nightEventsLog: document.getElementById("night-events-log"),
  nightSummaryBox: document.getElementById("night-summary-box"),
  nightReportSelect: document.getElementById("night-report-select"),
  nightReportViewer: document.getElementById("night-report-viewer"),
  nightReportButton: document.getElementById("btn-view-report"),
  expandNightEvents: document.getElementById("expand-night-events"),
  expandReportViewer: document.getElementById("expand-report-viewer"),
  modalOverlay: document.getElementById("modal-overlay"),
  modalText: document.getElementById("modal-text"),
  modalClose: document.getElementById("modal-close-btn"),
  startNightBtn: document.getElementById("start-night-btn"),
  stopNightBtn: document.getElementById("stop-night-btn"),
  chatForm: document.getElementById("chat-form"),
  chatMessage: document.getElementById("chat-message"),
  chatSend: document.getElementById("chat-send-btn"),
  chatTimeline: document.getElementById("chat-timeline"),
};

let currentSuggestionSession = null;
const discussionSessions = new Map();
let allPods = [];
let podsRendered = 0;
const PAGE_SIZE = 50;
let currentAgentSessionId = null;
let lastAgentCard = null;

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

function openModal(text) {
  if (!els.modalOverlay || !els.modalText) return;
  els.modalText.textContent = text || "";
  els.modalOverlay.classList.remove("hidden");
}

function closeModal() {
  if (!els.modalOverlay) return;
  els.modalOverlay.classList.add("hidden");
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

function updateNightStatus(isRunning) {
  const pill = els.nightStatusPill;
  if (!pill) return;
  if (isRunning) {
    pill.textContent = "● Running";
    pill.classList.add("night-status-running");
    pill.classList.remove("night-status-stopped");
  } else {
    pill.textContent = "● Stopped";
    pill.classList.add("night-status-stopped");
    pill.classList.remove("night-status-running");
  }
}

function updateOverallHealth(unhealthyPercent = 0, severity = "") {
  if (!els.overallHealth) return;
  const normalized = severity.toString().toLowerCase();
  let status = "GOOD";
  let tone = "pill-good";

  if (normalized === "high" || normalized === "critical") {
    status = "CRITICAL";
    tone = "pill-critical";
  } else if (normalized === "medium" || normalized === "warn" || unhealthyPercent > 20) {
    status = "WARN";
    tone = "pill-warn";
  }

  els.overallHealth.textContent = `Overall Health: ${status}`;
  els.overallHealth.classList.remove("pill-good", "pill-warn", "pill-critical");
  els.overallHealth.classList.add(tone);
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

function renderNamespaceRowsWithBar(target, rows, valueKey, emptyText = "No data") {
  if (!target) return;
  target.innerHTML = "";
  const columnCount = target.closest("table")?.querySelectorAll("th").length || 3;
  if (!Array.isArray(rows) || rows.length === 0) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="${columnCount}" class="muted">${emptyText}</td>`;
    target.appendChild(tr);
    return;
  }

  const maxValue = Math.max(
    ...rows.map((row) => {
      const value = Number(row[valueKey]);
      return Number.isFinite(value) ? value : 0;
    }),
    0,
  );

  rows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.className = "namespace-row";
    const value = Number(row[valueKey]);
    const fill = maxValue > 0 && Number.isFinite(value) ? value / maxValue : 0;
    tr.style.setProperty("--fill", fill);

    tr.innerHTML = `
      <td>
        <div class="namespace-row-bar"></div>
        <span class="ns-name">${row.namespace || ""}</span>
      </td>
      <td class="ns-metric">${row[valueKey] ?? 0}</td>
      <td class="ns-pods">${row.pods ?? 0}</td>
    `;
    target.appendChild(tr);
  });
}

function renderPodRow(pod) {
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td>${pod.namespace || ""}</td>
    <td>${pod.name || ""}</td>
    <td>${pod.status || ""}</td>
    <td>${pod.restarts ?? ""}</td>
    <td>${pod.age || ""}</td>
    <td>${pod.node || ""}</td>
  `;
  return tr;
}

function renderNextPods() {
  if (!els.podsTableBody || !Array.isArray(allPods)) return;
  if (podsRendered >= allPods.length) return;

  const nextBatch = allPods.slice(podsRendered, podsRendered + PAGE_SIZE);
  nextBatch.forEach((pod) => {
    els.podsTableBody.appendChild(renderPodRow(pod));
  });
  podsRendered += nextBatch.length;
}

function setupPodsInfiniteScroll() {
  if (!els.podsTableContainer) return;
  els.podsTableContainer.addEventListener("scroll", () => {
    const { scrollTop, clientHeight, scrollHeight } = els.podsTableContainer;
    if (scrollTop + clientHeight >= scrollHeight - 16) {
      renderNextPods();
    }
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

    if (els.metricsUpdated) {
      els.metricsUpdated.textContent = new Date().toLocaleTimeString();
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

    const severity = alerts.last_severity || "—";
    if (els.lastSeverity) {
      els.lastSeverity.textContent = severity === "—" ? "—" : severity.toString().toUpperCase();
    }

    updateOverallHealth(podsSummary.unhealthy_percent ?? badPercent, severity);
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

    renderNamespaceRowsWithBar(
      els.nsTopCpu,
      data.namespaces?.top_by_cpu || [],
      "cpu_mcores",
    );

    renderNamespaceRowsWithBar(
      els.nsTopMemory,
      data.namespaces?.top_by_memory || [],
      "memory_mib",
    );

    renderNamespaceRows(
      els.nsUnhealthy,
      (data.namespaces?.unhealthy_counts || []).map((item) => ({
        Namespace: item.namespace || "",
        Unhealthy: item.unhealthy_pods ?? 0,
      })),
      "All namespaces healthy",
    );

    allPods = pods;
    podsRendered = 0;
    if (els.podsTableBody) {
      els.podsTableBody.innerHTML = "";
    }
    if (els.podsTableContainer) {
      els.podsTableContainer.scrollTop = 0;
    }
    renderNextPods();

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
    updateNightStatus(Boolean(data.running));
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

async function loadNightSummary() {
  if (!els.nightSummaryBox) return;
  try {
    const resp = await fetch("/api/night/summary");
    if (!resp.ok) throw new Error(`Failed to load summary: ${resp.status}`);
    const data = await resp.json();
    els.nightSummaryBox.textContent = data.summary_markdown || "(No summary available)";
  } catch (err) {
    console.error(err);
    els.nightSummaryBox.textContent = "Unable to load Night Mode summary.";
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
      updateNightStatus(true);
    } else if (endpoint.includes("stop")) {
      notify("Night Mode stopped");
      updateNightStatus(false);
    }
    return data;
  } catch (err) {
    console.error(err);
    notify("Night action failed", "error");
  }
}

async function loadSreSuggestions() {
  const listEl = document.getElementById("suggestions-list");
  if (!listEl) return;
  listEl.innerText = "Loading suggestions…";

  try {
    const res = await fetch("/api/sre/suggestions");
    const data = await res.json();
    const suggestions = data.suggestions || [];
    if (suggestions.length === 0) {
      listEl.innerText = "No active suggestions. Cluster looks calm.";
      return;
    }

    listEl.innerHTML = "";
    suggestions.forEach((s) => {
      const card = document.createElement("div");
      card.className = "suggestion-card";
      card.dataset.suggestionId = s.id;

      card.innerHTML = `
        <div class="suggestion-title">${s.title}</div>
        <div class="suggestion-meta">
          <span class="suggestion-risk suggestion-risk-${s.risk || "low"}">
            Risk: ${(s.risk || "low").toUpperCase()}
          </span>
          <span class="suggestion-category">${s.category || ""}</span>
        </div>
        <p class="suggestion-reason">${s.reason}</p>
        <p class="suggestion-action"><strong>Action:</strong> ${s.action}</p>
        <pre class="suggestion-command">${s.command}</pre>
        <div class="suggestion-result"></div>
        <div class="suggestion-actions">
          <button class="apply-btn">Apply</button>
          <button class="discuss-btn">Discuss</button>
          <button class="dismiss-btn">Dismiss</button>
        </div>
      `;
      listEl.appendChild(card);
    });
  } catch (err) {
    listEl.innerText = "Failed to load suggestions.";
    console.error(err);
  }
}

async function handleApplySuggestion(card, payload, resultEl) {
  card.classList.add("running");
  if (resultEl) {
    resultEl.innerHTML = '<span class="suggestion-running-indicator">Running recommended action…</span>';
  }
  try {
    const res = await fetch("/api/sre/suggestions/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    card.classList.remove("running");
    if (resultEl) {
      const icon = data.status === "ok" ? "✅" : "⚠️";
      const summary = data.summary || "No summary available.";
      resultEl.innerHTML = `<p><strong>${icon} Result:</strong> ${summary}</p>`;
    }
  } catch (err) {
    card.classList.remove("running");
    if (resultEl) {
      resultEl.innerHTML = `<p><strong>⚠️ Result:</strong> Failed to run action.</p>`;
    }
    console.error("Apply suggestion failed", err);
  }
}

function handleAgentResponseFromSuggestion(data) {
  if (typeof handleAgentResponse === "function") {
    handleAgentResponse(data);
    return;
  }

  currentSuggestionSession = data?.session_id || null;
  if (currentSuggestionSession) {
    currentAgentSessionId = currentSuggestionSession;
  }

  if (els.chatTimeline) {
    renderAgentStep(data, { reset: true });
  }
}

function buildDiscussionPayload(
  card,
  fallback = { title: "Discussion", reason: "", action: "", command: "" },
) {
  if (!card) return fallback;

  if (card.classList.contains("suggestion-card")) {
    const title = card.querySelector(".suggestion-title")?.innerText || "Suggestion";
    const reason = card.querySelector(".suggestion-reason")?.innerText || "";
    const actionText =
      card.querySelector(".suggestion-action")?.innerText.replace(/^Action:\s*/i, "") || "";
    const commandText = card.querySelector(".suggestion-command")?.innerText || "";
    return {
      title,
      reason,
      action: actionText,
      command: commandText,
    };
  }

  if (card.classList.contains("agent-step-card")) {
    const title = card.querySelector(".entry-label")?.innerText || "SRE Step";
    const reason = card.querySelector(".entry-title")?.innerText || card.dataset.command || "";
    const actionText = card.querySelector(".agent-status")?.innerText || "";
    const commandText = card.dataset.command || card.querySelector(".command-text")?.innerText || "";
    return {
      title,
      reason,
      action: actionText,
      command: commandText,
    };
  }

  if (card.classList.contains("final-entry")) {
    const reason = card.querySelector(".final-answer-text")?.innerText || "";
    return {
      title: "Final Answer",
      reason,
      action: "Final outcome",
      command: "",
    };
  }

  return fallback;
}

function ensureDiscussionPanel(card) {
  let discussion = card.querySelector(".suggestion-discussion");
  if (!discussion) {
    discussion = document.createElement("div");
    discussion.className = "suggestion-discussion";
    discussion.innerHTML = `
      <div class="suggestion-discussion-log"></div>
      <div class="suggestion-discussion-input">
        <textarea placeholder="Ask about this item…"></textarea>
        <button class="suggestion-send-btn">Send</button>
      </div>
    `;
    card.appendChild(discussion);
  }

  const logEl = discussion.querySelector(".suggestion-discussion-log");
  const textarea = discussion.querySelector("textarea");
  const sendBtn = discussion.querySelector(".suggestion-send-btn");

  if (!discussion.dataset.bound && textarea && sendBtn) {
    sendBtn.addEventListener("click", () => sendSuggestionMessage(card));
    textarea.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        sendSuggestionMessage(card);
      }
    });
    discussion.dataset.bound = "true";
  }

  if (textarea) textarea.focus();

  return { discussion, logEl };
}

async function openSuggestionDiscussion(card, payload) {
  const { logEl } = ensureDiscussionPanel(card);

  if (!discussionSessions.has(card)) {
    try {
      const res = await fetch("/api/sre/suggestions/chat/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      discussionSessions.set(card, data.session_id);
      appendSuggestionMessage(
        logEl,
        "assistant",
        data.assistant || "Let’s discuss this item.",
      );
    } catch (err) {
      appendSuggestionMessage(
        logEl,
        "assistant",
        "I couldn't start a discussion session for this item.",
      );
      console.error("start suggestion chat failed", err);
    }
  }
}

function appendSuggestionMessage(logEl, role, text) {
  if (!logEl) return;
  const div = document.createElement("div");
  div.className = role === "user" ? "msg-user" : "msg-assistant";
  div.innerHTML = `<span class="msg-label">${role === "user" ? "You" : "SRE"}</span> ${text}`;
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
}

async function sendSuggestionMessage(card) {
  const sessionId = discussionSessions.get(card);
  if (!sessionId) return;
  const discussion = card.querySelector(".suggestion-discussion");
  if (!discussion) return;
  const logEl = discussion.querySelector(".suggestion-discussion-log");
  const textarea = discussion.querySelector("textarea");
  if (!logEl || !textarea) return;
  const text = textarea.value.trim();
  if (!text) return;
  textarea.value = "";
  appendSuggestionMessage(logEl, "user", text);

  try {
    const res = await fetch("/api/sre/suggestions/chat/step", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message: text }),
    });
    const data = await res.json();
    appendSuggestionMessage(logEl, "assistant", data.assistant || "[No answer]");
  } catch (err) {
    appendSuggestionMessage(
      logEl,
      "assistant",
      "Error while replying in this discussion.",
    );
    console.error("suggestion chat step failed", err);
  }
}

function scrollTimeline() {
  if (!els.chatTimeline) return;
  els.chatTimeline.scrollTop = els.chatTimeline.scrollHeight;
}

function resetTimeline() {
  if (els.chatTimeline) {
    els.chatTimeline.innerHTML = "";
  }
}

function appendUserMessage(text) {
  if (!els.chatTimeline) return;
  const card = document.createElement("div");
  card.className = "chat-entry user-entry";
  card.innerHTML = `
    <p class="entry-label">You</p>
    <p class="entry-text">${text}</p>
  `;
  els.chatTimeline.appendChild(card);
  scrollTimeline();
}

function appendFinalAnswer(answerText) {
  if (!els.chatTimeline) return;
  const card = document.createElement("div");
  card.className = "chat-entry final-entry";
  card.innerHTML = `
    <p class="entry-label">Final Answer</p>
    <p class="final-answer-text">${answerText || ""}</p>
    <div class="discussion-trigger-row">
      <button type="button" class="card-discuss-btn">Discuss</button>
    </div>
  `;
  els.chatTimeline.appendChild(card);
  scrollTimeline();
}

function setAgentStatus(card, label, toneClass = "") {
  const status = card?.querySelector?.(".agent-status");
  if (!status) return;
  status.textContent = label;
  status.className = "agent-status";
  if (toneClass) {
    status.classList.add(toneClass);
  }
}

function setAgentButtonsDisabled(card, disabled) {
  const buttons = card?.querySelectorAll?.("button[data-agent-action]");
  if (!buttons) return;
  buttons.forEach((btn) => {
    btn.disabled = disabled;
  });
}

function setAgentOutput(card, summaryText, highlights = [], exitCode = null) {
  const output = card?.querySelector?.(".agent-output");
  if (!output) return;
  const highlightHtml = Array.isArray(highlights)
    ? highlights.map((line) => `<li>${line}</li>`).join("")
    : "";

  const completionChip = document.createElement("span");
  completionChip.className = "agent-chip subtle-pill";
  completionChip.textContent = exitCode == null
    ? "Command completed"
    : `Command completed (exit code ${exitCode})`;

  output.innerHTML = `
    <p class="command-summary">${summaryText || "Command executed."}</p>
    ${highlightHtml ? `<ul class="command-highlights">${highlightHtml}</ul>` : ""}
  `;
  output.prepend(completionChip);
  setAgentStatus(card, "Completed", "status-done");
  setAgentButtonsDisabled(card, true);
}

function setAgentRunning(card, label = "Running…") {
  const output = card?.querySelector?.(".agent-output");
  if (output) {
    output.innerHTML = `
      <div class="agent-running">
        <span class="dot-pulse"></span>
        <span>${label}</span>
      </div>
    `;
  }
  setAgentStatus(card, "Running", "status-running");
  setAgentButtonsDisabled(card, true);
}

function appendOutcomeToCard(card, answerText) {
  if (!card) return;
  const outcome = document.createElement("div");
  outcome.className = "agent-outcome";
  outcome.innerHTML = `
    <p class="entry-label">Outcome</p>
    <p class="final-answer-text">${answerText || ""}</p>
  `;
  card.appendChild(outcome);
  scrollTimeline();
}

function createAgentStepCard(data, options = {}) {
  if (!els.chatTimeline) return null;
  const { showActions = true, statusLabel = "Awaiting approval", statusTone = "status-awaiting" } = options;
  const commandText = data.command || data.proposed_command || data?.ran?.command || "";

  const card = document.createElement("div");
  card.className = "chat-entry agent-step-card";
  card.dataset.command = commandText;

  const header = document.createElement("div");
  header.className = "agent-card-header";
  header.innerHTML = `
    <p class="entry-label">${data.header || "SRE Step"}</p>
    <span class="agent-status ${statusTone}">${statusLabel}</span>
  `;

  const title = document.createElement("p");
  title.className = "entry-title";
  title.textContent = data.reason || data.note || "Proposed command";

  const commandBlock = document.createElement("pre");
  commandBlock.className = "command-text";
  commandBlock.textContent = commandText || data.note || "";

  const output = document.createElement("div");
  output.className = "agent-output muted";
  output.textContent = showActions ? "Awaiting your decision" : "Preparing…";

  const actions = document.createElement("div");
  actions.className = `proposal-actions ${showActions ? "" : "hidden"}`.trim();

  [
    { label: "Approve", action: "approve" },
    { label: "Deny", action: "deny" },
    { label: "Skip", action: "skip" },
  ].forEach((btnDef) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = btnDef.label;
    btn.dataset.agentAction = btnDef.action;
    btn.dataset.command = commandText;
    actions.appendChild(btn);
  });

  card.appendChild(header);
  card.appendChild(title);
  card.appendChild(commandBlock);
  card.appendChild(output);
  card.appendChild(actions);

  const discussRow = document.createElement("div");
  discussRow.className = "discussion-trigger-row";
  discussRow.innerHTML = `<button type="button" class="card-discuss-btn">Discuss</button>`;
  card.appendChild(discussRow);

  els.chatTimeline.appendChild(card);
  scrollTimeline();

  return card;
}

function renderAgentStep(data, { reset = false } = {}) {
  if (!els.chatTimeline || !data) return;
  if (reset) {
    resetTimeline();
    lastAgentCard = null;
  }

  const commandText = data.command || data.proposed_command || data?.ran?.command || "";
  const hasCommandOutput = Boolean(data.command_output);
  const isProposal =
    data.status === "need_approval" || (data.status === "intermediate" && data.proposed_command);

  if (isProposal) {
    lastAgentCard = createAgentStepCard(data, {
      showActions: true,
      statusLabel: "Awaiting approval",
      statusTone: "status-awaiting",
    });
    return;
  }

  if (data.status === "intermediate" && !data.proposed_command) {
    lastAgentCard = createAgentStepCard(
      { ...data, command: commandText, reason: data.note || data.reason || "Update" },
      { showActions: false, statusLabel: "In progress", statusTone: "status-running" },
    );
    return;
  }

  if (data.status === "running_command") {
    if (!lastAgentCard) {
      lastAgentCard = createAgentStepCard(data, {
        showActions: false,
        statusLabel: "Running",
        statusTone: "status-running",
      });
    }
    setAgentRunning(lastAgentCard);
  }

  if (hasCommandOutput) {
    if (!lastAgentCard) {
      lastAgentCard = createAgentStepCard(data, {
        showActions: false,
        statusLabel: "Running",
        statusTone: "status-running",
      });
    }
    setAgentOutput(lastAgentCard, data.command_output, data.highlights || [], data.ran?.exit_code);
  }

  if (data.status === "done") {
    const finalText = data.final_answer || data.answer || "";
    if (finalText) {
      if (lastAgentCard && (commandText || hasCommandOutput || data.ran)) {
        appendOutcomeToCard(lastAgentCard, finalText);
      } else {
        appendFinalAnswer(finalText);
      }
    }
    if (lastAgentCard) {
      setAgentStatus(lastAgentCard, "Completed", "status-done");
      setAgentButtonsDisabled(lastAgentCard, true);
    }
    currentAgentSessionId = null;
  }
}

async function pollNextAgentStep() {
  if (!currentAgentSessionId) return;
  let keepGoing = true;
  while (keepGoing && currentAgentSessionId) {
    const res = await fetch("/api/agent/step", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: currentAgentSessionId, decision: {} }),
    });
    const data = await res.json();
    renderAgentStep(data);

    if (data.status === "intermediate" || data.status === "running_command") {
      keepGoing = true;
    } else {
      keepGoing = false;
    }
  }
}

async function handleAgentDecision(decisionType, commandText, card) {
  if (!currentAgentSessionId) return;

  const targetCard = card || lastAgentCard;
  if (targetCard) {
    lastAgentCard = targetCard;
    if (decisionType === "approve") {
      setAgentRunning(targetCard, "Running…");
    } else {
      setAgentButtonsDisabled(targetCard, true);
      setAgentStatus(targetCard, "Processing", "status-running");
      const output = targetCard.querySelector(".agent-output");
      if (output) {
        output.innerHTML = `
          <div class="agent-running">
            <span class="dot-pulse"></span>
            <span>Processing…</span>
          </div>
        `;
      }
    }
  }

  const res = await fetch("/api/agent/step", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: currentAgentSessionId,
      decision: { type: decisionType, command: commandText },
    }),
  });
  const data = await res.json();
  renderAgentStep(data);
  if (data.status === "intermediate" || data.status === "running_command") {
    await pollNextAgentStep();
  }
}

async function startAgentFromChat() {
  const message = (els.chatMessage.value || "").trim();
  if (!message) {
    notify("Please enter a message", "error");
    return;
  }

  if (els.chatMessage) {
    els.chatMessage.value = "";
  }

  currentAgentSessionId = null;
  resetTimeline();
  lastAgentCard = null;
  appendUserMessage(message);

  try {
    const res = await fetch("/api/agent/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: message }),
    });
    const data = await res.json();
    currentAgentSessionId = data.session_id || null;
    renderAgentStep(data);
    if (data.status === "intermediate" || data.status === "running_command") {
      await pollNextAgentStep();
    }
  } catch (err) {
    console.error(err);
    notify("Chat request failed", "error");
  }
}

function setupNightButtons() {
  if (els.startNightBtn) {
    els.startNightBtn.addEventListener("click", () => {
      postNightAction(NIGHT_START_ENDPOINT);
    });
  }
  if (els.stopNightBtn) {
    els.stopNightBtn.addEventListener("click", () => {
      postNightAction(NIGHT_STOP_ENDPOINT);
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

function setupModal() {
  if (els.modalClose) {
    els.modalClose.addEventListener("click", () => closeModal());
  }

  if (els.modalOverlay) {
    els.modalOverlay.addEventListener("click", (ev) => {
      if (ev.target === els.modalOverlay) {
        closeModal();
      }
    });
  }
}

function setupExpanders() {
  if (els.expandNightEvents) {
    els.expandNightEvents.addEventListener("click", () => {
      openModal(els.nightEventsLog?.innerText || "");
    });
  }

  if (els.expandReportViewer) {
    els.expandReportViewer.addEventListener("click", () => {
      openModal(els.nightReportViewer?.innerText || "");
    });
  }
}

function setupChat() {
  if (els.chatSend) {
    els.chatSend.addEventListener("click", (ev) => {
      ev.preventDefault();
      startAgentFromChat();
    });
  }

  if (els.chatMessage) {
    els.chatMessage.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        startAgentFromChat();
      }
    });
  }

  if (els.chatTimeline) {
    els.chatTimeline.addEventListener("click", async (ev) => {
      const discussBtn = ev.target.closest(".card-discuss-btn");
      if (discussBtn) {
        const card = ev.target.closest(".agent-step-card, .final-entry");
        const payload = buildDiscussionPayload(card);
        await openSuggestionDiscussion(card, payload);
        return;
      }

      const btn = ev.target.closest("button[data-agent-action]");
      if (!btn) return;
      const action = btn.dataset.agentAction;
      const card = btn.closest(".agent-step-card");
      const command = btn.dataset.command || card?.dataset.command || "";
      await handleAgentDecision(action, command, card);
    });
  }
}

function setupSuggestions() {
  const refreshBtn = document.getElementById("refresh-suggestions");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", (ev) => {
      ev.preventDefault();
      loadSreSuggestions();
    });
  }

  document.getElementById("suggestions-list")?.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const card = e.target.closest(".suggestion-card");
    if (!card) return;

    const title = card.querySelector(".suggestion-title")?.innerText || "";
    const reason = card.querySelector(".suggestion-reason")?.innerText || "";
    const actionText = card
      .querySelector(".suggestion-action")
      ?.innerText.replace(/^Action:\s*/i, "")
      || "";
    const cmd = card.querySelector(".suggestion-command")?.innerText || "";
    const resultEl = card.querySelector(".suggestion-result");

    const payload = { title, reason, action: actionText, command: cmd };

    if (btn.classList.contains("dismiss-btn")) {
      card.remove();
      return;
    }

    if (btn.classList.contains("apply-btn")) {
      await handleApplySuggestion(card, payload, resultEl);
      return;
    }

    if (btn.classList.contains("discuss-btn")) {
      await openSuggestionDiscussion(card, payload);
      return;
    }
  });

  loadSreSuggestions();
}

function startPolling() {
  setInterval(loadClusterPods, REFRESH_INTERVAL_MS);
  setInterval(loadNightStatus, REFRESH_INTERVAL_MS);
  setInterval(loadNightEvents, REFRESH_INTERVAL_MS);
  setInterval(loadNightSummary, SUMMARY_REFRESH_MS);
}

window.addEventListener("DOMContentLoaded", () => {
  setupPodsInfiniteScroll();
  loadClusterPods();
  loadNightStatus();
  loadNightEvents();
  loadNightReports();
  loadNightSummary();
  setupNightButtons();
  setupNightReports();
  setupModal();
  setupExpanders();
  setupChat();
  setupSuggestions();
  startPolling();
});
