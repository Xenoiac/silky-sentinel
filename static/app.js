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
  cpuDetail: document.getElementById("cpu-cores"),
  cpuGauge: document.getElementById("cpu-gauge"),
  memoryPercent: document.getElementById("memory-util"),
  memoryDetail: document.getElementById("memory-usage"),
  memoryGauge: document.getElementById("memory-gauge"),
  storagePercent: document.getElementById("storage-util"),
  storageDetail: document.getElementById("storage-usage"),
  storageGauge: document.getElementById("storage-gauge"),
  networkPercent: document.getElementById("network-util"),
  networkDetail: document.getElementById("network-usage"),
  networkGauge: document.getElementById("network-gauge"),
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
  reliabilityCard: document.getElementById("reliability-card"),
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

function setProgressBar(fill, percent = 0) {
  const safePercent = Number.isFinite(percent) ? Math.max(0, Math.min(100, percent)) : 0;
  const tone = gaugeTone(safePercent);
  if (!fill) return;
  fill.style.width = `${safePercent}%`;
  fill.classList.remove("tone-green", "tone-yellow", "tone-orange", "tone-red");
  fill.classList.add(tone);
}

class Gauge {
  constructor(container, { min = 0, max = 100, label = "", initial = 0 } = {}) {
    this.container = container;
    this.min = min;
    this.max = max;
    this.label = label;
    this.value = this.clamp(initial);
    this.animationFrame = null;

    if (!this.container) return;

    this.shell = document.createElement("div");
    this.shell.className = "gauge-shell";
    this.canvas = document.createElement("canvas");
    this.canvas.className = "gauge-canvas";
    this.shell.appendChild(this.canvas);

    const overlay = document.createElement("div");
    overlay.className = "gauge-overlay";
    this.valueEl = document.createElement("div");
    this.valueEl.className = "gauge-value-text";
    this.labelEl = document.createElement("div");
    this.labelEl.className = "gauge-label-text";
    this.labelEl.textContent = this.label;
    overlay.appendChild(this.valueEl);
    overlay.appendChild(this.labelEl);

    this.shell.appendChild(overlay);
    this.container.appendChild(this.shell);

    this.ctx = this.canvas.getContext("2d");
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(this.container);
    this.resize();
    this.draw(this.value);
  }

  clamp(val) {
    const safe = Number.isFinite(val) ? val : this.min;
    return Math.min(this.max, Math.max(this.min, safe));
  }

  resize() {
    const { width } = this.container.getBoundingClientRect();
    const height = Math.max(118, Math.min(170, width * 0.6));
    this.canvas.width = width * 2;
    this.canvas.height = height * 2;
    this.canvas.style.height = `${height}px`;
    this.canvas.style.width = `${width}px`;
    this.draw(this.value);
  }

  angleForPercent(percent) {
    const start = Math.PI;
    const end = 0;
    return start + (percent / 100) * (end - start);
  }

  draw(value) {
    if (!this.ctx) return;
    const ctx = this.ctx;
    const { width, height } = this.canvas;
    ctx.clearRect(0, 0, width, height);

    const centerX = width / 2;
    const centerY = height * 0.9;
    const radius = Math.min(width, height) * 0.42;
    const lineWidth = Math.max(12, radius * 0.14);

    ctx.lineCap = "round";

    const segments = [
      { start: 0, end: 70, color: "#22c55e" },
      { start: 70, end: 85, color: "#f59e0b" },
      { start: 85, end: 100, color: "#ef4444" },
    ];

    ctx.beginPath();
    ctx.strokeStyle = "#1f2937";
    ctx.lineWidth = lineWidth;
    ctx.arc(centerX, centerY, radius, Math.PI, 0, false);
    ctx.stroke();

    segments.forEach((segment) => {
      ctx.beginPath();
      ctx.strokeStyle = segment.color;
      ctx.lineWidth = lineWidth;
      ctx.shadowColor = "rgba(0,0,0,0.35)";
      ctx.shadowBlur = 8;
      ctx.arc(
        centerX,
        centerY,
        radius,
        this.angleForPercent(segment.start),
        this.angleForPercent(segment.end),
        false,
      );
      ctx.stroke();
      ctx.shadowBlur = 0;
    });

    const percent = ((value - this.min) / (this.max - this.min)) * 100;
    const clampedPercent = Math.min(100, Math.max(0, percent));
    const needleAngle = this.angleForPercent(clampedPercent);
    const needleLength = radius * 0.88;

    ctx.save();
    ctx.translate(centerX, centerY);
    ctx.rotate(needleAngle - Math.PI);
    ctx.beginPath();
    ctx.moveTo(-4, 0);
    ctx.lineTo(needleLength, 0);
    ctx.strokeStyle = "#e2e8f0";
    ctx.lineWidth = Math.max(2, radius * 0.04);
    ctx.shadowColor = "rgba(255, 255, 255, 0.15)";
    ctx.shadowBlur = 6;
    ctx.stroke();
    ctx.restore();

    ctx.beginPath();
    ctx.fillStyle = "#0f172a";
    ctx.strokeStyle = "#e2e8f0";
    ctx.lineWidth = 3;
    ctx.arc(centerX, centerY, lineWidth * 0.45, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();

    if (this.valueEl) {
      this.valueEl.textContent = `${Math.round(clampedPercent)}%`;
    }
  }

  setValue(next) {
    const target = this.clamp(next);
    const start = this.value;
    const diff = target - start;
    const duration = 520;
    const startTime = performance.now();

    const animate = (now) => {
      const progress = Math.min(1, (now - startTime) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      const current = start + diff * eased;
      this.value = current;
      this.draw(current);
      if (progress < 1) {
        this.animationFrame = requestAnimationFrame(animate);
      }
    };

    cancelAnimationFrame(this.animationFrame);
    this.animationFrame = requestAnimationFrame(animate);
  }
}

class NetworkTrafficSimulator {
  constructor({ capacityMbps = 120, variance = 3, spikeChance = 0.08 } = {}) {
    this.capacityMbps = capacityMbps;
    this.variance = variance;
    this.spikeChance = spikeChance;
    this.baseRx = this.randomBetween(8, 18);
    this.baseTx = this.randomBetween(6, 16);
    this.rx = this.baseRx;
    this.tx = this.baseTx;
  }

  randomBetween(min, max) {
    return min + Math.random() * (max - min);
  }

  percentFor(rx, tx) {
    const rxUtil = Math.min(1, rx / this.capacityMbps);
    const txUtil = Math.min(1, tx / this.capacityMbps);
    return Math.min(100, ((rxUtil + txUtil) / 2) * 100);
  }

  nudgeTowardsBaseline(current, base) {
    const drift = current * 0.82 + base * 0.18;
    const noise = this.randomBetween(-this.variance, this.variance);
    let next = drift + noise;
    if (Math.random() < this.spikeChance) {
      next += base * this.randomBetween(0.25, 0.55);
    }
    return Math.max(0, next);
  }

  nextSample() {
    this.rx = this.nudgeTowardsBaseline(this.rx, this.baseRx);
    this.tx = this.nudgeTowardsBaseline(this.tx, this.baseTx);
    return { rx: this.rx, tx: this.tx, percent: this.percentFor(this.rx, this.tx) };
  }

  seed(count = 40) {
    const samples = [];
    for (let i = 0; i < count; i += 1) {
      samples.push(this.nextSample());
    }
    return samples;
  }

  tuneTo(rx, tx) {
    if (Number.isFinite(rx)) {
      this.rx = rx;
      this.baseRx = rx;
    }
    if (Number.isFinite(tx)) {
      this.tx = tx;
      this.baseTx = tx;
    }
  }
}

class NetworkHistoryChart {
  constructor(container, { capacity = 60 } = {}) {
    this.container = container;
    this.capacity = capacity;
    this.rx = [];
    this.tx = [];
    this.canvas = null;
    this.ctx = null;
    this.dpr = window.devicePixelRatio || 1;

    if (this.container) {
      this.canvas = this.container.querySelector("canvas") || document.createElement("canvas");
      if (!this.canvas.parentElement) {
        this.container.appendChild(this.canvas);
      }
      this.ctx = this.canvas.getContext("2d");
      this.resizeObserver = new ResizeObserver(() => this.resize());
      this.resizeObserver.observe(this.container);
      this.resize();
    }
  }

  resize() {
    if (!this.container || !this.canvas || !this.ctx) return;
    const { width, height: containerHeight } = this.container.getBoundingClientRect();
    const targetHeight = containerHeight || width * 0.3;
    const height = Math.max(52, Math.min(90, targetHeight));
    this.dpr = window.devicePixelRatio || 1;
    this.canvas.width = width * this.dpr;
    this.canvas.height = height * this.dpr;
    this.canvas.style.width = `${width}px`;
    this.canvas.style.height = `${height}px`;
    this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    this.draw();
  }

  setData(samples = []) {
    this.rx = [];
    this.tx = [];
    samples.forEach((sample) => this.addSample(sample, false));
    this.draw();
  }

  addSample(sample, redraw = true) {
    if (!sample) return;
    if (Number.isFinite(sample.rx)) this.rx.push(sample.rx);
    if (Number.isFinite(sample.tx)) this.tx.push(sample.tx);
    if (this.rx.length > this.capacity) this.rx.shift();
    if (this.tx.length > this.capacity) this.tx.shift();
    if (redraw) this.draw();
  }

  drawLine(series, color, width, points) {
    if (!this.ctx || series.length < 2) return;
    const ctx = this.ctx;
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    series.forEach((value, idx) => {
      const { x, y } = points(idx, value);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  draw() {
    if (!this.ctx || !this.canvas) return;
    const ctx = this.ctx;
    const width = this.canvas.width / this.dpr;
    const height = this.canvas.height / this.dpr;
    ctx.clearRect(0, 0, width, height);

    const padding = 10;
    const usableWidth = Math.max(10, width - padding * 2);
    const usableHeight = Math.max(10, height - padding * 2);
    const maxVal = Math.max(...this.rx, ...this.tx, 1);
    const paddedMax = maxVal * 1.2;

    const valueToY = (val) => padding + usableHeight - (val / paddedMax) * usableHeight;
    const indexToX = (idx, total) => padding + (total <= 1 ? usableWidth : (idx / (total - 1)) * usableWidth);

    ctx.strokeStyle = "rgba(255,255,255,0.08)";
    ctx.lineWidth = 1;
    const gridLines = 3;
    for (let i = 0; i <= gridLines; i += 1) {
      const y = padding + (usableHeight / gridLines) * i;
      ctx.beginPath();
      ctx.moveTo(padding, y);
      ctx.lineTo(width - padding, y);
      ctx.stroke();
    }

    ctx.fillStyle = "rgba(226,232,240,0.7)";
    ctx.font = "10px 'Inter', system-ui, -apple-system, sans-serif";
    ctx.textAlign = "right";
    ctx.fillText(`${Math.round(paddedMax)} Mbps`, width - padding, padding + 8);

    const totalPoints = Math.max(this.rx.length, this.tx.length);
    const pointMapper = (idx, val) => ({ x: indexToX(idx, totalPoints), y: valueToY(val) });

    this.drawLine(this.rx, "#38bdf8", 2, (idx, val) => pointMapper(idx, val));
    this.drawLine(this.tx, "#a78bfa", 2, (idx, val) => pointMapper(idx, val));

    ctx.fillStyle = "rgba(226,232,240,0.8)";
    ctx.font = "9px 'Inter', system-ui, -apple-system, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText("RX", padding + 2, padding + 10);
    ctx.fillStyle = "rgba(226,232,240,0.7)";
    ctx.fillText("TX", padding + 26, padding + 10);
  }
}

class SimpleTimeseriesChart {
  constructor(
    wrapper,
    {
      seriesConfigs = [],
      points = 13,
      intervalMinutes = 5,
      yUnit = "",
      clampMax = null,
    } = {},
  ) {
    this.wrapper = wrapper;
    this.canvas = wrapper?.querySelector("canvas") || null;
    this.ctx = this.canvas?.getContext("2d") || null;
    this.seriesConfigs = seriesConfigs.map((cfg) => ({ ...cfg, values: [] }));
    this.points = points;
    this.intervalMs = intervalMinutes * 60 * 1000;
    this.yUnit = yUnit;
    this.clampMax = clampMax;
    this.timestamps = [];
    this.dpr = window.devicePixelRatio || 1;

    if (!this.canvas || !this.ctx || !this.wrapper) return;

    this.seedData();
    this.resize();

    const resizeObserver = new ResizeObserver(() => this.resize());
    resizeObserver.observe(this.wrapper);
  }

  seedData() {
    const now = Date.now();
    for (let i = this.points - 1; i >= 0; i -= 1) {
      const ts = new Date(now - i * this.intervalMs);
      this.timestamps.push(ts);
      this.seriesConfigs.forEach((cfg) => {
        const prev = cfg.values[cfg.values.length - 1];
        const next = this.generateValue(cfg, prev);
        cfg.values.push(next);
      });
    }
  }

  generateValue(cfg = {}, prev = null) {
    const base = Number.isFinite(cfg.base) ? cfg.base : Number.isFinite(prev) ? prev : 50;
    const variance = Number.isFinite(cfg.variance) ? cfg.variance : 4;
    const min = Number.isFinite(cfg.min) ? cfg.min : 0;
    const max = Number.isFinite(cfg.max) ? cfg.max : 100;
    const spikeChance = Number.isFinite(cfg.spikeChance) ? cfg.spikeChance : 0.08;
    const spikeScale = Number.isFinite(cfg.spikeScale) ? cfg.spikeScale : 0.18;

    const drift = (Number.isFinite(prev) ? prev : base) * 0.62 + base * 0.38;
    let next = drift + (Math.random() - 0.5) * variance;
    if (Math.random() < spikeChance) {
      next += Math.abs(base) * (spikeScale + Math.random() * spikeScale);
    }
    return Math.max(min, Math.min(max, next));
  }

  resize() {
    if (!this.canvas || !this.ctx || !this.wrapper) return;
    const { width, height: containerHeight } = this.wrapper.getBoundingClientRect();
    const targetHeight = containerHeight || width * 0.45;
    const height = Math.max(120, Math.min(220, targetHeight));
    this.dpr = window.devicePixelRatio || 1;
    this.canvas.width = width * this.dpr;
    this.canvas.height = height * this.dpr;
    this.canvas.style.width = `${width}px`;
    this.canvas.style.height = `${height}px`;
    this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    this.draw();
  }

  appendPoint(values = {}) {
    if (!this.canvas || !this.ctx) return;
    const ts = new Date();
    this.timestamps.push(ts);
    this.seriesConfigs.forEach((cfg) => {
      const incoming = Number(values?.[cfg.key]);
      const prev = cfg.values[cfg.values.length - 1];
      const next = Number.isFinite(incoming) ? incoming : this.generateValue(cfg, prev);
      cfg.values.push(next);
      if (cfg.values.length > this.points) cfg.values.shift();
    });
    if (this.timestamps.length > this.points) this.timestamps.shift();
    this.draw();
  }

  draw() {
    if (!this.canvas || !this.ctx) return;
    const ctx = this.ctx;
    const width = this.canvas.width / this.dpr;
    const height = this.canvas.height / this.dpr;
    ctx.clearRect(0, 0, width, height);

    const padding = 14;
    const usableWidth = Math.max(12, width - padding * 2);
    const usableHeight = Math.max(24, height - padding * 2);
    const allValues = this.seriesConfigs.flatMap((cfg) => cfg.values);
    const maxVal = Math.max(Math.max(...allValues, 1), 1);
    const targetMax = this.clampMax ? Math.min(this.clampMax, Math.max(maxVal, this.clampMax * 0.65)) : maxVal;
    const paddedMax = targetMax * 1.1;

    const valueToY = (val) => padding + usableHeight - (val / paddedMax) * usableHeight;
    const indexToX = (idx, total) => padding + (total <= 1 ? usableWidth : (idx / (total - 1)) * usableWidth);

    ctx.strokeStyle = "rgba(255,255,255,0.08)";
    ctx.lineWidth = 1;
    const gridLines = 3;
    for (let i = 0; i <= gridLines; i += 1) {
      const y = padding + (usableHeight / gridLines) * i;
      ctx.beginPath();
      ctx.moveTo(padding, y);
      ctx.lineTo(width - padding, y);
      ctx.stroke();
    }

    const totalPoints = this.timestamps.length;
    const pointMapper = (idx, val) => ({ x: indexToX(idx, totalPoints), y: valueToY(val) });

    this.seriesConfigs.forEach((cfg) => {
      if (!cfg.values || cfg.values.length < 2) return;
      ctx.beginPath();
      ctx.strokeStyle = cfg.color || "#38bdf8";
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      cfg.values.forEach((val, idx) => {
        const { x, y } = pointMapper(idx, val);
        if (idx === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    });

    ctx.fillStyle = "rgba(226,232,240,0.8)";
    ctx.font = "10px 'Inter', system-ui, -apple-system, sans-serif";
    ctx.textAlign = "right";
    ctx.fillText(`${Math.round(paddedMax)}${this.yUnit}`, width - padding + 4, padding + 6);

    ctx.textAlign = "left";
    ctx.fillStyle = "rgba(148,163,184,0.9)";
    if (totalPoints > 1) {
      ctx.fillText(this.formatTime(this.timestamps[0]), padding, height - 8);
      ctx.textAlign = "right";
      ctx.fillText(this.formatTime(this.timestamps[totalPoints - 1]), width - padding, height - 8);
    }

    if (this.seriesConfigs.length > 1) {
      ctx.textAlign = "left";
      this.drawLegend(ctx);
    }
  }

  drawLegend(ctx) {
    let x = 12;
    const y = 16;
    this.seriesConfigs.forEach((cfg, idx) => {
      ctx.fillStyle = cfg.color || "#38bdf8";
      ctx.beginPath();
      ctx.arc(x, y, 3.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "rgba(226,232,240,0.8)";
      ctx.font = "10px 'Inter', system-ui, -apple-system, sans-serif";
      ctx.textAlign = "left";
      ctx.fillText(cfg.label || `S${idx + 1}`, x + 7, y + 3.5);
      x += 7 + ctx.measureText(cfg.label || `S${idx + 1}`).width + 16;
    });
  }

  formatTime(date = new Date()) {
    return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(date);
  }
}

class ReliabilityCharts {
  constructor(container, { points = 36, intervalMinutes = 10, sloTarget = 99.5, threshold = 72 } = {}) {
    this.container = container;
    this.sliWrapper = container?.querySelector('[data-chart="sli"]');
    this.burnWrapper = container?.querySelector('[data-chart="burn"]');
    this.sliCanvas = this.sliWrapper?.querySelector("canvas");
    this.burnCanvas = this.burnWrapper?.querySelector("canvas");
    this.sliCtx = this.sliCanvas?.getContext("2d") || null;
    this.burnCtx = this.burnCanvas?.getContext("2d") || null;
    this.points = points;
    this.intervalMs = intervalMinutes * 60 * 1000;
    this.sloTarget = sloTarget;
    this.threshold = threshold;
    this.timestamps = [];
    this.sliValues = [];
    this.burnValues = [];
    this.timer = null;
    this.dpr = window.devicePixelRatio || 1;

    if (!this.sliCanvas || !this.burnCanvas) return;

    this.seedData();
    this.resize();
    this.draw();

    const resizeObserver = new ResizeObserver(() => this.resize());
    if (this.sliWrapper) resizeObserver.observe(this.sliWrapper);
    if (this.burnWrapper) resizeObserver.observe(this.burnWrapper);

    this.start();
  }

  seedData() {
    const now = Date.now();
    let lastSli = this.sloTarget + 0.1;
    let lastBurn = 18 + Math.random() * 6;
    for (let i = this.points - 1; i >= 0; i -= 1) {
      const ts = new Date(now - i * this.intervalMs);
      this.timestamps.push(ts);
      lastSli = this.generateSliValue(lastSli);
      lastBurn = this.generateBurnValue(lastBurn);
      this.sliValues.push(lastSli);
      this.burnValues.push(lastBurn);
    }
  }

  generateSliValue(prev = this.sloTarget) {
    const base = Number.isFinite(prev) ? prev : this.sloTarget;
    const drift = (Math.random() - 0.5) * 0.35;
    const dip = Math.random() < 0.14 ? -(0.3 + Math.random()) : 0;
    const next = base + drift + dip;
    return Math.min(100, Math.max(97, next));
  }

  generateBurnValue(prev = 12) {
    const base = Number.isFinite(prev) ? prev : 12;
    const drift = (Math.random() - 0.45) * 1.1;
    const spike = Math.random() < 0.16 ? Math.random() * 6 : 0;
    const next = base + drift + spike;
    return Math.min(100, Math.max(0, next));
  }

  resizeCanvas(wrapper, canvas) {
    if (!wrapper || !canvas) return;
    const { width, height } = wrapper.getBoundingClientRect();
    this.dpr = window.devicePixelRatio || 1;
    canvas.width = width * this.dpr;
    canvas.height = height * this.dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
  }

  resize() {
    this.resizeCanvas(this.sliWrapper, this.sliCanvas);
    this.resizeCanvas(this.burnWrapper, this.burnCanvas);
    this.draw();
  }

  formatTime(ts) {
    const d = ts instanceof Date ? ts : new Date(ts);
    const hours = d.getHours().toString().padStart(2, "0");
    const minutes = d.getMinutes().toString().padStart(2, "0");
    return `${hours}:${minutes}`;
  }

  drawGrid(ctx, width, height, { min, max, padding = 10 }) {
    const usableHeight = Math.max(10, height - padding * 2);
    ctx.strokeStyle = "rgba(255,255,255,0.08)";
    ctx.lineWidth = 1;
    const steps = 4;
    for (let i = 0; i <= steps; i += 1) {
      const y = padding + (usableHeight / steps) * i;
      ctx.beginPath();
      ctx.moveTo(padding, y);
      ctx.lineTo(width - padding, y);
      ctx.stroke();
    }

    ctx.fillStyle = "rgba(226,232,240,0.7)";
    ctx.font = "10px 'Inter', system-ui, -apple-system, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText(`${max.toFixed(1)}%`, padding, padding + 10);
    ctx.textAlign = "right";
    ctx.fillText(`${min.toFixed(1)}%`, width - padding, height - padding + 4);
  }

  drawLine(ctx, values, mapper, color, width = 2, dashed = false) {
    if (!ctx || values.length < 2) return;
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    if (dashed) ctx.setLineDash([5, 6]);
    values.forEach((value, idx) => {
      const { x, y } = mapper(idx, value);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    if (dashed) ctx.setLineDash([]);
  }

  drawSliChart() {
    if (!this.sliCtx || !this.sliCanvas) return;
    const ctx = this.sliCtx;
    const width = this.sliCanvas.width / this.dpr;
    const height = this.sliCanvas.height / this.dpr;
    ctx.clearRect(0, 0, width, height);

    const padding = 12;
    const min = 97;
    const max = 100;
    const usableWidth = Math.max(10, width - padding * 2);
    const usableHeight = Math.max(10, height - padding * 2);

    this.drawGrid(ctx, width, height, { min, max, padding });

    const valueToY = (val) => padding + usableHeight - ((val - min) / (max - min)) * usableHeight;
    const indexToX = (idx) => padding + (idx / Math.max(1, this.sliValues.length - 1)) * usableWidth;

    const sloY = valueToY(this.sloTarget);
    ctx.strokeStyle = "rgba(251,191,36,0.9)";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.setLineDash([4, 6]);
    ctx.moveTo(padding, sloY);
    ctx.lineTo(width - padding, sloY);
    ctx.stroke();
    ctx.setLineDash([]);

    this.drawLine(
      ctx,
      this.sliValues,
      (idx, val) => ({ x: indexToX(idx), y: valueToY(val) }),
      "#22c55e",
      2.4,
    );

    ctx.fillStyle = "rgba(226,232,240,0.8)";
    ctx.font = "10px 'Inter', system-ui, -apple-system, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText(this.formatTime(this.timestamps[0]), padding, height - 6);
    ctx.textAlign = "right";
    ctx.fillText(this.formatTime(this.timestamps[this.timestamps.length - 1]), width - padding, height - 6);
  }

  drawBurnChart() {
    if (!this.burnCtx || !this.burnCanvas) return;
    const ctx = this.burnCtx;
    const width = this.burnCanvas.width / this.dpr;
    const height = this.burnCanvas.height / this.dpr;
    ctx.clearRect(0, 0, width, height);

    const padding = 12;
    const min = 0;
    const max = 100;
    const usableWidth = Math.max(10, width - padding * 2);
    const usableHeight = Math.max(10, height - padding * 2);

    this.drawGrid(ctx, width, height, { min, max, padding });

    const valueToY = (val) => padding + usableHeight - ((val - min) / (max - min)) * usableHeight;
    const indexToX = (idx) => padding + (idx / Math.max(1, this.burnValues.length - 1)) * usableWidth;

    const thresholdY = valueToY(this.threshold);
    ctx.strokeStyle = "rgba(251,191,36,0.9)";
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    ctx.setLineDash([4, 6]);
    ctx.moveTo(padding, thresholdY);
    ctx.lineTo(width - padding, thresholdY);
    ctx.stroke();
    ctx.setLineDash([]);

    this.drawLine(
      ctx,
      this.burnValues,
      (idx, val) => ({ x: indexToX(idx), y: valueToY(val) }),
      "#a855f7",
      2.2,
    );

    ctx.fillStyle = "rgba(226,232,240,0.8)";
    ctx.font = "10px 'Inter', system-ui, -apple-system, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText(this.formatTime(this.timestamps[0]), padding, height - 6);
    ctx.textAlign = "right";
    ctx.fillText(this.formatTime(this.timestamps[this.timestamps.length - 1]), width - padding, height - 6);
  }

  draw() {
    this.drawSliChart();
    this.drawBurnChart();
  }

  appendPoint() {
    const lastTime = this.timestamps[this.timestamps.length - 1] || new Date();
    const nextTime = new Date(lastTime.getTime() + this.intervalMs);
    const nextSli = this.generateSliValue(this.sliValues[this.sliValues.length - 1]);
    const nextBurn = this.generateBurnValue(this.burnValues[this.burnValues.length - 1]);

    this.timestamps.push(nextTime);
    this.sliValues.push(nextSli);
    this.burnValues.push(nextBurn);

    if (this.timestamps.length > this.points) this.timestamps.shift();
    if (this.sliValues.length > this.points) this.sliValues.shift();
    if (this.burnValues.length > this.points) this.burnValues.shift();
  }

  start() {
    this.stop();
    this.timer = setInterval(() => {
      this.appendPoint();
      this.draw();
    }, 30000);
  }

  stop() {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}

const gauges = {};
const charts = {};
const networkSimulator = new NetworkTrafficSimulator();

function initGauges() {
  gauges.cpu = new Gauge(els.cpuGauge, { label: "CPU", initial: 0 });
  gauges.memory = new Gauge(els.memoryGauge, { label: "Memory", initial: 0 });
  gauges.storage = new Gauge(els.storageGauge, { label: "Storage", initial: 0 });
  gauges.network = new Gauge(els.networkGauge, { label: "Network", initial: 0 });
}

function initReliabilityCharts() {
  if (!els.reliabilityCard) return;
  charts.reliability = new ReliabilityCharts(els.reliabilityCard, {
    points: 36,
    intervalMinutes: 10,
    sloTarget: 99.5,
    threshold: 75,
  });
}

function initTimeseriesCharts() {
  charts.timeseries = charts.timeseries || {};

  const cpuWrapper = document.getElementById("cpu-timeseries-chart");
  const memWrapper = document.getElementById("memory-timeseries-chart");
  const netWrapper = document.getElementById("network-timeseries-chart");

  if (cpuWrapper) {
    charts.timeseries.cpu = new SimpleTimeseriesChart(cpuWrapper, {
      seriesConfigs: [
        { key: "cpu", label: "CPU", color: "#38bdf8", base: 58, variance: 5.5, min: 10, max: 98 },
      ],
      yUnit: "%",
      clampMax: 100,
    });
  }

  if (memWrapper) {
    charts.timeseries.memory = new SimpleTimeseriesChart(memWrapper, {
      seriesConfigs: [
        { key: "memory", label: "Memory", color: "#f59e0b", base: 62, variance: 6, min: 15, max: 98 },
      ],
      yUnit: "%",
      clampMax: 100,
    });
  }

  if (netWrapper) {
    charts.timeseries.network = new SimpleTimeseriesChart(netWrapper, {
      seriesConfigs: [
        { key: "rx", label: "RX", color: "#38bdf8", base: 32, variance: 6, min: 4, max: 150, spikeChance: 0.12 },
        { key: "tx", label: "TX", color: "#a78bfa", base: 28, variance: 5, min: 4, max: 150, spikeChance: 0.1 },
      ],
      yUnit: " Mbps",
      clampMax: 160,
    });
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

function applyNetworkSample(sample = {}) {
  const percent = Number.isFinite(sample.percent) ? sample.percent : 0;
  const rx = Number(sample.rx);
  const tx = Number(sample.tx);

  gauges.network?.setValue(percent);
  if (els.networkPercent) {
    els.networkPercent.textContent = `${percent.toFixed(1)}%`;
  }
  if (els.networkDetail) {
    if (Number.isFinite(rx) && Number.isFinite(tx)) {
      els.networkDetail.textContent = `${rx.toFixed(1)} / ${tx.toFixed(1)} Mbps (rx/tx)`;
    } else {
      els.networkDetail.textContent = "Live traffic";
    }
  }
}

function buildNetworkSample(networkData = {}) {
  const rx = Number(networkData?.rx_mbps);
  const tx = Number(networkData?.tx_mbps);
  const percentFromData = Number(networkData?.utilization_percent);

  if (Number.isFinite(rx) && Number.isFinite(tx)) {
    networkSimulator.tuneTo(rx, tx);
    return { rx, tx, percent: Number.isFinite(percentFromData) ? percentFromData : networkSimulator.percentFor(rx, tx) };
  }

  return networkSimulator.nextSample();
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

    const cpuPercent = Number(cpu.utilization_percent ?? 0);
    const memoryPercent = Number(memory.utilization_percent ?? 0);
    const storagePercent = Number(storage.utilization_percent ?? 0);
    const networkSample = buildNetworkSample(data.network);

    gauges.cpu?.setValue(cpuPercent);
    gauges.memory?.setValue(memoryPercent);
    gauges.storage?.setValue(storagePercent);
    applyNetworkSample(networkSample);

    charts.timeseries?.cpu?.appendPoint({ cpu: cpuPercent });
    charts.timeseries?.memory?.appendPoint({ memory: memoryPercent });
    charts.timeseries?.network?.appendPoint({ rx: networkSample.rx, tx: networkSample.tx });

    if (els.cpuPercent) {
      els.cpuPercent.textContent = `${cpuPercent.toFixed(1)}%`;
    }
    if (els.cpuDetail) {
      els.cpuDetail.textContent = `${(cpu.used_cores ?? 0).toFixed(2)} / ${(cpu.total_cores ?? 0).toFixed(2)} cores`;
    }
    if (els.memoryPercent) {
      els.memoryPercent.textContent = `${memoryPercent.toFixed(1)}%`;
    }
    if (els.memoryDetail) {
      els.memoryDetail.textContent = `${(memory.used_gib ?? 0).toFixed(2)} / ${(memory.total_gib ?? 0).toFixed(2)} GiB`;
    }
    if (els.storagePercent) {
      els.storagePercent.textContent = `${storagePercent.toFixed(1)}%`;
    }
    if (els.storageDetail) {
      els.storageDetail.textContent = `${(storage.used_gib ?? 0).toFixed(2)} / ${(storage.total_gib ?? 0).toFixed(2)} GiB`;
    }

    const total = podsSummary.total ?? pods.length;
    const bad = podsSummary.unhealthy ?? 0;
    const badPercent = total > 0 ? (bad / total) * 100 : 0;
    const podPercent = podsSummary.unhealthy_percent ?? badPercent;
    setProgressBar(els.podsFill, podPercent);
    if (els.podsPercent) {
      els.podsPercent.textContent = `${podPercent.toFixed(1)}%`;
    }
    if (els.podsDetail) {
      els.podsDetail.textContent = `${bad} unhealthy of ${total} pods`;
    }

    const notReady = nodes.not_ready ?? 0;
    const ready = nodes.ready ?? 0;
    const nodeTotal = nodes.count ?? ready + notReady;
    const notReadyPercent = nodeTotal > 0 ? (notReady / nodeTotal) * 100 : 0;
    setProgressBar(els.nodesFill, notReadyPercent);
    if (els.nodesPercent) {
      els.nodesPercent.textContent = `${notReadyPercent.toFixed(1)}%`;
    }
    if (els.nodesDetail) {
      els.nodesDetail.textContent = `${ready} ready / ${notReady} not-ready`;
    }

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
    setAgentRunning(lastAgentCard, data.running_message || "Running…");
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
  initGauges();
  initTimeseriesCharts();
  initReliabilityCharts();
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
