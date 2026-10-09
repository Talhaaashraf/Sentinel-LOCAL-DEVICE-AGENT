// ============================================================================
// DOM helpers
// ============================================================================

function $(id) {
  return document.getElementById(id);
}

function setText(id, value) {
  $(id).textContent = value;
}

function percent(value) {
  return typeof value === 'number' ? `${value.toFixed(1)}%` : '--';
}

function levelFor(value, warning = 75, critical = 90) {
  if (value >= critical) return 'critical';
  if (value >= warning) return 'warning';
  return 'ok';
}

// ============================================================================
// Shared state
// ============================================================================

let latestReport = null;
let socket = null;
let reconnectTimer = null;
let chatHistory = [];
let generatedToken = '';
let selectedPlatform = 'windows';

// ============================================================================
// Dashboard — live monitor
// ============================================================================

function renderReport(report) {
  latestReport = report;

  const cpu = report.cpu || {};
  const ram = report.memory?.ram || {};
  const disks = report.disk || [];
  const network = report.network || {};
  const battery = report.battery;
  const diskValue = disks.reduce((max, disk) => Math.max(max, disk.usage_percent || 0), 0);

  const gauges = [
    ['cpu', cpu.total_usage_percent, levelFor(cpu.total_usage_percent || 0, 75, 85)],
    ['ram', ram.usage_percent, levelFor(ram.usage_percent || 0, 75, 85)],
    ['disk', diskValue, levelFor(diskValue, 75, 90)],
  ];
  for (const [name, value, state] of gauges) {
    $(`${name}-gauge`).closest('.metric-card').className = `metric-card ${state}`;
    setText(`${name}-value`, typeof value === 'number' ? value.toFixed(1) : '--');
    setText(`${name}-state`, state.toUpperCase());
  }

  setText('cpu-detail', `${cpu.logical_cores || '--'} logical cores / ${cpu.current_frequency_mhz || '--'} MHz`);
  setText('ram-detail', `${ram.available || '--'} available`);
  setText('disk-detail', disks.length ? `${disks.length} mounted volume${disks.length === 1 ? '' : 's'}` : 'No volumes');

  const interfacesUp = (network.interfaces || []).filter((item) => item.status === 'UP').length;
  setText('network-value', network.interfaces ? `${interfacesUp} up` : '--');
  setText('network-state', 'LIVE');
  setText('network-detail', `${network.bytes_received || '--'} received`);

  setText('battery-value', battery ? percent(battery.percent) : 'N/A');
  setText('battery-state', battery ? (battery.charging ? 'CHARGING' : 'ON BATTERY') : 'N/A');
  setText('battery-detail', battery ? battery.time_remaining : 'No battery detected');

  setText('hostname', report.system?.hostname || '--');
  setText('os-name', `${report.system?.os_name || '--'} ${report.system?.os_release || ''}`);
  setText('uptime', report.system?.uptime || '--');
  setText('architecture', report.system?.architecture || '--');
  setText('process-count', `${report.processes_count ?? '--'} processes`);
  setText('last-updated', `Updated ${new Date().toLocaleTimeString()}`);

  setText('chat-host', report.system?.hostname || '--');
  setText('chat-cpu', percent(cpu.total_usage_percent));
  setText('chat-ram', percent(ram.usage_percent));
  setText('chat-disk', percent(diskValue));
}

function renderHealth(health) {
  const banner = $('health-banner');
  banner.className = `health-banner ${health.status}`;
  setText('health-title', health.status === 'ok' ? 'System healthy' : `${health.status} signals detected`);
  setText('health-message', health.message);
}

function setConnection(state, detail) {
  $('connection-state').className = `connection-state ${state}`;
  const label = state === 'connected' ? 'Monitor connected' : state === 'disconnected' ? 'Disconnected' : 'Connecting';
  setText('connection-label', label);
  setText('connection-detail', detail);
}

function connectMonitor() {
  clearTimeout(reconnectTimer);
  setConnection('', 'Opening monitor stream');
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${protocol}://${location.host}/ws/monitor`);
  socket.onopen = () => setConnection('connected', 'Live stream / 5 second interval');
  socket.onmessage = (event) => renderReport(JSON.parse(event.data));
  socket.onclose = () => {
    setConnection('disconnected', 'Retrying in 3 seconds');
    reconnectTimer = setTimeout(connectMonitor, 3000);
  };
  socket.onerror = () => socket.close();
}

async function loadHealth() {
  try {
    const response = await fetch('/api/diagnostics/health');
    if (!response.ok) throw new Error('Health unavailable');
    renderHealth(await response.json());
  } catch (error) {
    renderHealth({ status: 'critical', message: error.message });
  }
}

// ============================================================================
// Alerts
// ============================================================================

function renderDiagnosis(container, data) {
  container.replaceChildren();

  const summary = document.createElement('strong');
  summary.textContent = data.summary || data.plain_explanation || 'No summary returned.';
  container.appendChild(summary);

  const root = document.createElement('p');
  root.textContent = `Likely root cause: ${data.root_cause || data.technical_root_cause || 'Unknown'}`;
  container.appendChild(root);

  const list = document.createElement('ul');
  (data.steps || data.recommendations || []).forEach((step) => {
    const item = document.createElement('li');
    item.textContent = String(step);
    list.appendChild(item);
  });
  container.appendChild(list);
}

async function loadAlerts() {
  const body = $('alerts-body');
  try {
    const response = await fetch('/api/alerts');
    if (!response.ok) throw new Error('Alert history unavailable');
    const data = await response.json();
    setText('alert-count', data.alerts.length);

    if (!data.alerts.length) {
      body.innerHTML = '<tr><td colspan="5" class="empty-state">No alerts recorded. The local rules engine is watching.</td></tr>';
      return;
    }

    body.innerHTML = data.alerts.map((alert) => `
      <tr>
        <td>${new Date(alert.timestamp).toLocaleString()}</td>
        <td><span class="severity severity-${alert.severity}">${alert.severity}</span></td>
        <td>${alert.category}</td>
        <td>${alert.description}</td>
        <td><button class="diagnose-button" data-diagnose="${alert.id}">Diagnose with AI</button></td>
      </tr>
      <tr id="diagnosis-${alert.id}" class="diagnosis-row hidden">
        <td colspan="5"><div class="diagnosis" id="diagnosis-content-${alert.id}"></div></td>
      </tr>
    `).join('');
  } catch (error) {
    body.innerHTML = `<tr><td colspan="5" class="empty-state">${error.message}</td></tr>`;
  }
}

async function diagnoseAlert(id) {
  const row = $(`diagnosis-${id}`);
  const content = $(`diagnosis-content-${id}`);
  row.classList.remove('hidden');
  content.textContent = 'Consulting technician...';
  try {
    const response = await fetch('/api/agent/diagnose', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ diagnostics: latestReport, alert_history: [] }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'AI unavailable');
    renderDiagnosis(content, data);
  } catch (error) {
    content.textContent = error.message;
  }
}

// ============================================================================
// Security
// ============================================================================

const RISKY_PORTS = { 21: 'FTP', 23: 'Telnet', 445: 'SMB', 3389: 'RDP', 5900: 'VNC' };

function isLoopbackAddress(address) {
  return address === '127.0.0.1' || address === '::1' || address.startsWith('127.');
}

function setStatusCard(prefix, ok, detail, unknownMessage) {
  const card = $(`${prefix}-card`);
  const state = $(`${prefix}-state`);
  const detailElement = $(`${prefix}-detail`);
  if (ok === null || ok === undefined) {
    card.className = 'metric-card';
    state.textContent = 'UNKNOWN';
    detailElement.textContent = detail || unknownMessage;
    return;
  }
  card.className = `metric-card ${ok ? 'ok' : 'critical'}`;
  state.textContent = ok ? 'OK' : 'ATTENTION';
  detailElement.textContent = detail;
}

function renderSecurity(security) {
  const firewall = security.firewall || {};
  const antivirus = security.antivirus || {};
  const encryption = security.disk_encryption || {};
  const updates = security.updates || {};
  const admin = security.running_as_admin;

  setStatusCard('firewall', firewall.available ? firewall.enabled : null, firewall.detail, 'Not available on this platform');
  setStatusCard('antivirus', antivirus.available ? antivirus.enabled : null, antivirus.detail, 'Not available on this platform');
  setStatusCard('encryption', encryption.available ? encryption.encrypted : null, encryption.detail, 'Not available on this platform');

  $('updates-card').className = 'metric-card';
  setText('updates-state', updates.available ? 'CHECKED' : 'UNKNOWN');
  setText('updates-detail', updates.detail || 'Not available');

  $('admin-card').className = 'metric-card';
  setText('admin-state', admin === null || admin === undefined ? 'UNKNOWN' : admin ? 'ELEVATED' : 'STANDARD');
  setText('admin-detail', admin ? 'Running with administrator/root privileges' : 'Running with standard user privileges');

  const ports = (security.listening_ports || {}).ports || [];
  setText('ports-count', `${ports.length} open`);

  const body = $('ports-body');
  if (!ports.length) {
    body.innerHTML = '<tr><td colspan="4" class="empty-state">No listening ports detected.</td></tr>';
    return;
  }
  body.innerHTML = ports.map((port) => {
    const risky = RISKY_PORTS[port.port] && !isLoopbackAddress(port.address);
    const riskCell = risky ? `<span class="severity severity-high">${RISKY_PORTS[port.port]}</span>` : '<span class="soft-label">--</span>';
    return `<tr><td>${port.port}</td><td>${port.address}</td><td>${port.process}</td><td>${riskCell}</td></tr>`;
  }).join('');
}

async function loadSecurity() {
  try {
    const response = await fetch('/api/diagnostics/security');
    if (!response.ok) throw new Error('Security data unavailable');
    renderSecurity(await response.json());
  } catch (error) {
    $('ports-body').innerHTML = `<tr><td colspan="4" class="empty-state">${error.message}</td></tr>`;
  }
}

// ============================================================================
// Performance
// ============================================================================

function renderPerformance(performance) {
  const cpu = performance.cpu || {};
  const disk = performance.disk || {};
  const network = performance.network || {};

  $('perf-cpu-card').className = 'metric-card ok';
  setText('perf-cpu-state', 'TESTED');
  setText('perf-cpu-detail', cpu.detail || 'No result');

  $('perf-disk-card').className = `metric-card ${disk.available ? 'ok' : 'critical'}`;
  setText('perf-disk-state', disk.available ? 'TESTED' : 'FAILED');
  setText('perf-disk-detail', disk.detail || 'No result');

  $('perf-network-card').className = `metric-card ${network.available ? 'ok' : 'critical'}`;
  setText('perf-network-state', network.available ? 'TESTED' : 'UNREACHABLE');
  setText('perf-network-detail', network.detail || 'No result');

  setText('performance-status', `Last tested ${new Date().toLocaleTimeString()}`);
}

async function loadPerformance() {
  try {
    const response = await fetch('/api/diagnostics/performance');
    if (response.status === 404) return;
    if (!response.ok) throw new Error('Performance data unavailable');
    renderPerformance(await response.json());
  } catch (error) {
    setText('performance-status', error.message);
  }
}

async function runPerformanceTest() {
  const button = $('run-performance');
  button.disabled = true;
  setText('performance-status', 'Running benchmark — this briefly uses CPU, disk, and network...');
  try {
    const response = await fetch('/api/diagnostics/performance', { method: 'POST' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Benchmark failed');
    renderPerformance(data);
  } catch (error) {
    setText('performance-status', error.message);
  } finally {
    button.disabled = false;
  }
}

// ============================================================================
// Event Logs
// ============================================================================

function setLogCountCard(prefix, count, tone) {
  const card = $(`logs-${prefix}-card`);
  card.className = `metric-card ${count > 0 ? tone : 'ok'}`;
  setText(`logs-${prefix}-state`, count > 0 ? String(count) : 'CLEAR');
  setText(`logs-${prefix}-detail`, count > 0 ? `${count} in the last window` : 'None recorded');
}

function renderEventLogs(report) {
  setLogCountCard('critical', report.critical_count || 0, 'critical');
  setLogCountCard('error', report.error_count || 0, 'critical');
  setLogCountCard('warning', report.warning_count || 0, 'warning');

  const events = report.events || [];
  const body = $('logs-body');
  if (!report.available) {
    body.innerHTML = `<tr><td colspan="4" class="empty-state">${report.detail || 'Event log data unavailable on this platform.'}</td></tr>`;
    return;
  }
  if (!events.length) {
    body.innerHTML = `<tr><td colspan="4" class="empty-state">${report.detail || 'No warning, error, or critical events found.'}</td></tr>`;
    return;
  }
  body.innerHTML = events.map((event) => `
    <tr>
      <td>${event.time ? new Date(event.time).toLocaleString() : '--'}</td>
      <td><span class="severity severity-${(event.level || '').toLowerCase() === 'warning' ? 'medium' : 'critical'}">${event.level || 'Unknown'}</span></td>
      <td>${event.source || '--'}</td>
      <td>${event.message || ''}</td>
    </tr>
  `).join('');
}

async function loadEventLogs() {
  try {
    const response = await fetch('/api/diagnostics/logs');
    if (!response.ok) throw new Error('Event log data unavailable');
    renderEventLogs(await response.json());
  } catch (error) {
    $('logs-body').innerHTML = `<tr><td colspan="4" class="empty-state">${error.message}</td></tr>`;
  }
}

async function diagnoseEventLogs() {
  const output = $('logs-diagnosis');
  output.classList.remove('hidden');
  output.textContent = 'Consulting technician...';
  try {
    const response = await fetch('/api/diagnostics/logs/diagnose', { method: 'POST' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'AI unavailable');
    renderDiagnosis(output, data);
  } catch (error) {
    output.textContent = error.message;
  }
}

// ============================================================================
// Ask Technician (chat)
// ============================================================================

async function checkAI() {
  try {
    const status = await fetch('/api/ai/status').then((response) => response.json());
    $('ai-banner').classList.toggle('hidden', Boolean(status.available));
    if (!status.available) setText('ai-banner-detail', `${status.message} Local monitoring remains active.`);
    setText('ai-model-label', `AI: ${status.provider} / ${status.model || '--'} ${status.available ? '(ready)' : '(unavailable)'} / auto-fix: ${status.auto_fix_max_risk}`);
  } catch (_error) {
    $('ai-banner').classList.remove('hidden');
  }
}

function addChatMessage(text, role) {
  const message = document.createElement('div');
  message.className = `chat-message ${role}`;
  if (role === 'assistant' && text && typeof text === 'object') {
    renderDiagnosis(message, text);
  } else {
    message.textContent = String(text);
  }
  $('chat-messages').appendChild(message);
  $('chat-messages').scrollTop = $('chat-messages').scrollHeight;
}

async function sendChat(event) {
  event.preventDefault();
  const input = $('chat-input');
  const message = input.value.trim();
  if (!message) return;

  input.value = '';
  addChatMessage(message, 'user');

  try {
    const response = await fetch('/api/agent/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, diagnostics: latestReport, conversation_history: chatHistory }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `Chat request failed (${response.status})`);
    addChatMessage(data.reply, 'assistant');
    chatHistory.push({ role: 'user', content: message }, { role: 'assistant', content: data.reply });
    chatHistory = chatHistory.slice(-6);
  } catch (error) {
    console.error('Chat request failed:', error);
    addChatMessage(`Could not reach the server. ${error.message}`, 'assistant');
  }
}

// ============================================================================
// Agents — fleet list, device detail, provisioning
// ============================================================================

function statusPill(label, ok) {
  const state = ok === null || ok === undefined ? 'unknown' : ok ? 'ok' : 'critical';
  const text = ok === null || ok === undefined ? 'unknown' : ok ? 'ok' : 'attention';
  return `<span class="status-pill ${state}">${label}: ${text}</span>`;
}

function renderOsCountSummary(agentsList) {
  const summary = $('os-count-summary');
  if (!agentsList.length) {
    summary.innerHTML = '';
    return;
  }
  const counts = agentsList.reduce((totals, agent) => {
    const osName = agent.os_type || 'Unknown';
    totals[osName] = (totals[osName] || 0) + 1;
    return totals;
  }, {});
  const order = ['Windows', 'Darwin', 'Linux'];
  const labels = { Windows: 'Windows', Darwin: 'macOS', Linux: 'Linux' };
  const entries = Object.keys(counts).sort((a, b) => order.indexOf(a) - order.indexOf(b));
  summary.innerHTML = entries.map((osName) => `<span><b>${counts[osName]}</b>${labels[osName] || osName}</span>`).join('');
}

async function loadAgents() {
  const grid = $('agents-grid');
  try {
    const response = await fetch('/api/agents');
    if (!response.ok) throw new Error('Agent list unavailable');
    const data = await response.json();
    setText('agent-count', data.agents.length);
    renderOsCountSummary(data.agents);

    if (!data.agents.length) {
      grid.innerHTML = '<div class="empty-state">No devices connected yet - download the agent to get started.</div>';
      return;
    }

    grid.innerHTML = data.agents.map((agent) => {
      const osBadge = agent.os_type === 'Windows' ? 'W' : agent.os_type === 'Darwin' ? 'M' : 'L';
      return `
        <button class="agent-card" data-device="${agent.device_id}">
          <div class="agent-card-top">
            <span class="os-badge">${osBadge}</span>
            <span class="device-status ${agent.status}">${agent.status}</span>
          </div>
          <strong>${agent.nickname}</strong>
          <small>${agent.hostname} / ${agent.os_type}</small>
          <div class="agent-card-foot"><span>${new Date(agent.last_seen).toLocaleString()}</span><b>${agent.health.status}</b></div>
        </button>
      `;
    }).join('');
  } catch (error) {
    grid.innerHTML = `<div class="empty-state">${error.message}</div>`;
  }
}

async function fetchDeviceExtra(path) {
  const response = await fetch(path);
  if (!response.ok) return null;
  return response.json();
}

async function loadDevice(deviceId) {
  const detail = $('device-detail');
  detail.classList.remove('hidden');
  detail.innerHTML = '<p>Loading device snapshot...</p>';

  try {
    const [snapshot, alerts, security, performanceData] = await Promise.all([
      fetch(`/api/agents/${deviceId}/diagnostics`).then((response) => response.json()),
      fetch(`/api/agents/${deviceId}/alerts`).then((response) => response.json()),
      fetchDeviceExtra(`/api/agents/${deviceId}/security`),
      fetchDeviceExtra(`/api/agents/${deviceId}/performance`),
    ]);

    const report = snapshot.diagnostics;
    const disk = (report.disk || []).reduce((max, item) => Math.max(max, item.usage_percent || 0), 0);

    const securitySection = security
      ? `<div class="device-security"><h3>Security</h3>${statusPill('Firewall', security.firewall?.available ? security.firewall.enabled : null)}${statusPill('Antivirus', security.antivirus?.available ? security.antivirus.enabled : null)}${statusPill('Encryption', security.disk_encryption?.available ? security.disk_encryption.encrypted : null)}</div>`
      : '<div class="device-security"><h3>Security</h3><p class="soft-label">Not reported yet.</p></div>';

    const performanceSection = performanceData
      ? `<div class="device-performance"><h3>Performance</h3><p class="soft-label">${performanceData.cpu?.detail || ''}</p><p class="soft-label">${performanceData.disk?.detail || ''}</p><p class="soft-label">${performanceData.network?.detail || ''}</p></div>`
      : '<div class="device-performance"><h3>Performance</h3><p class="soft-label">Not reported yet.</p></div>';

    detail.innerHTML = `
      <div class="section-heading">
        <div><p class="kicker">SELECTED ENDPOINT</p><h2>${report.system?.hostname || deviceId}</h2></div>
        <div class="button-row"><button class="primary-button" data-device-troubleshoot="${deviceId}">Troubleshoot this device</button><button class="ghost-button" data-device-diagnose="${deviceId}">Quick AI summary</button></div>
      </div>
      <div class="mini-metrics">
        <div><small>CPU</small><strong>${percent(report.cpu?.total_usage_percent)}</strong></div>
        <div><small>RAM</small><strong>${percent(report.memory?.ram?.usage_percent)}</strong></div>
        <div><small>DISK</small><strong>${percent(disk)}</strong></div>
        <div><small>ALERTS</small><strong>${alerts.alerts.length}</strong></div>
      </div>
      ${securitySection}
      ${performanceSection}
      <div id="device-diagnosis" class="diagnosis"></div>
    `;
  } catch (error) {
    detail.innerHTML = `<p>${error.message}</p>`;
  }
}

async function diagnoseDevice(deviceId) {
  const output = $('device-diagnosis');
  output.textContent = 'Consulting technician...';
  try {
    const response = await fetch(`/api/agents/${deviceId}/agent-diagnose`, { method: 'POST' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'AI unavailable');
    renderDiagnosis(output, data);
  } catch (error) {
    output.textContent = error.message;
  }
}

// ============================================================================
// Add Agent — one-line enrollment
// ============================================================================

const INSTALL_COMMANDS = {
  windows: {
    hint: 'Open PowerShell as Administrator on the Windows device and run:',
    install: (server, token) => `irm "${server}/install/windows.ps1?token=${token}&server=${encodeURIComponent(server)}" | iex`,
    uninstall: (server) => `irm "${server}/install/uninstall-windows.ps1" | iex`,
  },
  linux: {
    hint: 'Open a terminal on the Linux device and run (needs sudo):',
    install: (server, token) => `curl -fsSL "${server}/install/linux.sh?token=${token}&server=${encodeURIComponent(server)}" | sudo bash`,
    uninstall: (server) => `curl -fsSL "${server}/install/uninstall-linux.sh" | sudo bash`,
  },
  mac: {
    hint: 'Open Terminal on the Mac and run (needs an admin password):',
    install: (server, token) => `curl -fsSL "${server}/install/macos.sh?token=${token}&server=${encodeURIComponent(server)}" | sudo bash`,
    uninstall: (server) => `curl -fsSL "${server}/install/uninstall-macos.sh" | sudo bash`,
  },
};

let enrollWaitTimer = null;

function enrollServer() {
  return ($('enroll-server').value.trim() || location.origin).replace(/\/+$/, '');
}

function isLocalOnlyServer(server) {
  try {
    const host = new URL(server).hostname;
    return host === 'localhost' || host === '::1' || host === '[::1]' || host.startsWith('127.');
  } catch (_error) {
    return true;
  }
}

function updateInstallInstructions() {
  const server = enrollServer();
  const commands = INSTALL_COMMANDS[selectedPlatform];
  $('enroll-server-warning').classList.toggle('hidden', !isLocalOnlyServer(server));
  setText('install-hint', commands.hint);
  setText('uninstall-command', commands.uninstall(server));
  setText('install-command', generatedToken ? commands.install(server, generatedToken) : 'Generate a token first.');
  $('copy-install').disabled = !generatedToken;
}

async function generateToken() {
  const result = $('token-result');
  try {
    const response = await fetch('/api/agents/generate-token', {
      method: 'POST',
      headers: { 'X-Admin-Key': $('admin-key-input').value },
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Admin authentication failed');
    generatedToken = data.token;
    result.textContent = `Token ready (single use, expires ${new Date(data.expires_at).toLocaleString()})`;
    result.classList.remove('hidden');
    updateInstallInstructions();
    waitForNewDevice();
  } catch (error) {
    result.textContent = error.message;
    result.classList.remove('hidden');
  }
}

async function waitForNewDevice() {
  clearInterval(enrollWaitTimer);
  const known = new Set(((await fetch('/api/agents').then((response) => response.json()).catch(() => ({ agents: [] }))).agents || []).map((agent) => agent.device_id));
  $('enroll-wait').classList.remove('hidden');
  setText('enroll-wait-text', 'Waiting for the device to connect... run the command above on it.');
  enrollWaitTimer = setInterval(async () => {
    try {
      const data = await fetch('/api/agents').then((response) => response.json());
      const fresh = (data.agents || []).find((agent) => !known.has(agent.device_id));
      if (fresh) {
        clearInterval(enrollWaitTimer);
        setText('enroll-wait-text', `Connected: ${fresh.nickname} (${fresh.hostname}, ${fresh.os_type}). You can now troubleshoot it from "What's the issue?".`);
        setText('agent-count', data.agents.length);
      }
    } catch (_error) {
      // keep waiting
    }
  }, 4000);
}

async function copyInstallCommand() {
  const text = $('install-command').textContent;
  try {
    await navigator.clipboard.writeText(text);
    setText('copy-install', 'Copied');
  } catch (_error) {
    const range = document.createRange();
    range.selectNodeContents($('install-command'));
    getSelection().removeAllRanges();
    getSelection().addRange(range);
    setText('copy-install', 'Press Ctrl+C');
  }
  setTimeout(() => setText('copy-install', 'Copy'), 2000);
}

async function downloadAgent() {
  const result = $('install-instructions');
  result.classList.remove('hidden');
  result.textContent = 'Checking for a built installer...';
  try {
    const response = await fetch(`/api/agents/download/${selectedPlatform}`);
    const contentType = response.headers.get('content-type') || '';
    if (!response.ok || contentType.includes('application/json')) {
      const data = await response.json();
      result.textContent = `${data.message}\n\nManual fallback:\n${data.manual_install}`;
      return;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = selectedPlatform === 'windows' ? 'DeviceHealthAgent.exe' : 'DeviceHealthAgent';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    result.textContent = 'Downloaded. Place agent_config.json beside the binary before starting it.';
  } catch (error) {
    console.error('Agent download failed:', error);
    result.textContent = `Download failed: ${error.message}\n\nManual fallback: pip install -r agent_requirements.txt && python monitor_agent.py`;
  }
}

function loadAddAgent() {
  if (!$('enroll-server').value) $('enroll-server').value = location.origin;
  updateInstallInstructions();
}

// ============================================================================
// What's the issue? — agentic troubleshooting
// ============================================================================

const TERMINAL_STATUSES = ['diagnosed', 'resolved', 'unresolved', 'failed'];
const STATUS_LABELS = {
  queued: 'Queued', running: 'Investigating', diagnosed: 'Diagnosed', fixing: 'Applying fix',
  verifying: 'Verifying', resolved: 'Resolved', unresolved: 'Not resolved', failed: 'Failed',
};
const STATUS_TONES = { resolved: 'ok', diagnosed: 'ok', unresolved: 'critical', failed: 'critical' };

let tsSocket = null;
let tsPollTimer = null;
let tsSessionId = null;
let tsPlaybookId = null;
let tsPlaybooks = [];
let tsRefreshPending = false;
let tsPendingStep = null;

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
}

function toolLabel(toolId) {
  const text = String(toolId || '').replace(/_/g, ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function argsLabel(args) {
  const entries = Object.entries(args || {});
  return entries.length ? ` (${entries.map(([key, value]) => `${key}: ${value}`).join(', ')})` : '';
}

async function loadTroubleshoot() {
  loadTroubleshootTargets();
  loadTroubleshootHistory();
  if (!tsPlaybooks.length) {
    try {
      tsPlaybooks = (await fetch('/api/troubleshoot/playbooks').then((response) => response.json())).playbooks || [];
    } catch (_error) {
      tsPlaybooks = [];
    }
    $('ts-playbooks').innerHTML = tsPlaybooks.map((playbook) => `<button class="playbook-chip" data-playbook="${escapeHtml(playbook.id)}">${escapeHtml(playbook.title)}</button>`).join('');
  }
}

async function loadTroubleshootTargets(selectedId) {
  const select = $('ts-device');
  const current = selectedId || select.value || 'local';
  try {
    const data = await fetch('/api/troubleshoot/targets').then((response) => response.json());
    select.innerHTML = (data.targets || []).map((target) => {
      const offline = target.status !== 'online' ? ' - offline' : '';
      return `<option value="${escapeHtml(target.device_id)}">${escapeHtml(target.label)} / ${escapeHtml(target.os_type)}${offline}</option>`;
    }).join('');
    select.value = [...select.options].some((option) => option.value === current) ? current : 'local';
  } catch (_error) {
    select.innerHTML = '<option value="local">This server</option>';
  }
}

async function loadTroubleshootHistory() {
  const list = $('ts-history');
  try {
    const data = await fetch('/api/troubleshoot/sessions?limit=15').then((response) => response.json());
    if (!data.sessions.length) {
      list.innerHTML = '<p class="soft-label">No sessions yet.</p>';
      return;
    }
    list.innerHTML = data.sessions.map((session) => `
      <button class="ts-history-item" data-session="${escapeHtml(session.session_id)}">
        <span class="status-pill ${STATUS_TONES[session.status] || 'unknown'}">${escapeHtml(STATUS_LABELS[session.status] || session.status)}</span>
        <strong>${escapeHtml(session.issue)}</strong>
        <small>${escapeHtml(session.device_label || session.device_id)} / ${new Date(session.created_at).toLocaleString()}</small>
      </button>`).join('');
  } catch (error) {
    list.innerHTML = `<p class="soft-label">${escapeHtml(error.message)}</p>`;
  }
}

function selectPlaybook(playbookId) {
  const playbook = tsPlaybooks.find((item) => item.id === playbookId);
  if (!playbook) return;
  const same = tsPlaybookId === playbookId;
  tsPlaybookId = same ? null : playbookId;
  document.querySelectorAll('.playbook-chip').forEach((chip) => chip.classList.toggle('active', chip.dataset.playbook === tsPlaybookId));
  if (!same) $('ts-issue').value = playbook.issue;
}

async function startTroubleshoot() {
  const issue = $('ts-issue').value.trim();
  if (!issue) {
    setText('ts-form-status', 'Describe the issue or pick a common problem first.');
    return;
  }
  const button = $('ts-start');
  button.disabled = true;
  setText('ts-form-status', 'Starting the AI agent...');
  try {
    const response = await fetch('/api/troubleshoot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ issue, device_id: $('ts-device').value, playbook_id: tsPlaybookId }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not start troubleshooting');
    setText('ts-form-status', '');
    openTroubleshootSession(data.session_id);
    loadTroubleshootHistory();
  } catch (error) {
    setText('ts-form-status', error.message);
  } finally {
    button.disabled = false;
  }
}

function openTroubleshootSession(sessionId) {
  tsSessionId = sessionId;
  tsPendingStep = null;
  $('ts-session').classList.remove('hidden');
  $('ts-steps').innerHTML = '';
  $('ts-report').innerHTML = '<p class="soft-label">Loading session...</p>';
  connectTroubleshootStream(sessionId);
  $('ts-session').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function connectTroubleshootStream(sessionId) {
  if (tsSocket) tsSocket.close();
  clearInterval(tsPollTimer);
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${protocol}://${location.host}/ws/troubleshoot/${sessionId}`);
  tsSocket = socket;
  socket.onmessage = (event) => {
    if (sessionId !== tsSessionId) return;
    const message = JSON.parse(event.data);
    if (message.type === 'snapshot') renderTroubleshootSession(message.session);
    else if (message.type === 'step_started') {
      tsPendingStep = message;
      $('ts-steps').appendChild(pendingStepElement(message));
    } else {
      if (message.type === 'step' || message.type === 'error') tsPendingStep = null;
      refreshTroubleshootSession();
    }
  };
  socket.onclose = () => {
    if (tsSocket !== socket || sessionId !== tsSessionId) return;
    // Fall back to polling until the session settles.
    tsPollTimer = setInterval(async () => {
      const session = await refreshTroubleshootSession();
      if (session && TERMINAL_STATUSES.includes(session.status)) clearInterval(tsPollTimer);
    }, 3000);
  };
}

async function refreshTroubleshootSession() {
  if (!tsSessionId || tsRefreshPending) return null;
  tsRefreshPending = true;
  try {
    const response = await fetch(`/api/troubleshoot/${tsSessionId}`);
    if (!response.ok) return null;
    const session = await response.json();
    if (session.session_id === tsSessionId) renderTroubleshootSession(session);
    if (TERMINAL_STATUSES.includes(session.status)) loadTroubleshootHistory();
    return session;
  } finally {
    tsRefreshPending = false;
  }
}

function pendingStepElement(message) {
  const item = document.createElement('li');
  item.className = 'ts-step pending';
  const verb = message.kind === 'fix' ? 'Applying fix' : message.kind === 'verify' ? 'Re-checking' : 'Running';
  item.innerHTML = `<span class="ts-step-icon"></span><div><strong>${verb}: ${escapeHtml(toolLabel(message.tool_id))}${escapeHtml(argsLabel(message.args))}</strong><small>working...</small></div>`;
  return item;
}

function renderStep(step) {
  if (step.kind === 'thought') {
    return `<li class="ts-step thought"><span class="ts-step-icon"></span><div><small>AI</small><p>${escapeHtml(step.text)}</p></div></li>`;
  }
  const result = step.result || {};
  const prefix = step.kind === 'fix' ? 'FIX ' : step.kind === 'verify' ? 'RE-CHECK ' : '';
  return `<li class="ts-step ${result.ok ? 'ok' : 'failed'} ${step.kind}">
    <span class="ts-step-icon"></span>
    <div>
      <strong>${prefix}${escapeHtml(toolLabel(step.tool_id))}${escapeHtml(argsLabel(step.args))}</strong>
      <p>${escapeHtml(result.summary || 'No result')}</p>
      <details><summary>Raw data</summary><pre>${escapeHtml(JSON.stringify(result.data, null, 2) || 'null')}</pre></details>
    </div>
  </li>`;
}

function renderFix(fix, index, session) {
  const busy = !TERMINAL_STATUSES.includes(session.status);
  const done = fix.status === 'applied';
  const label = done ? 'Applied' : fix.status === 'running' ? 'Applying...' : fix.status === 'failed' ? 'Retry' : 'Apply fix';
  const result = fix.result ? `<p class="ts-fix-result ${fix.result.ok ? 'ok' : 'failed'}">${escapeHtml(fix.result.summary)}</p>` : '';
  return `<div class="ts-fix">
    <div class="ts-fix-head">
      <strong>${escapeHtml(toolLabel(fix.tool_id))}${escapeHtml(argsLabel(fix.args))}</strong>
      <span class="risk risk-${escapeHtml(fix.risk)}">${escapeHtml(fix.risk)} risk</span>
    </div>
    <p>${escapeHtml(fix.why || fix.description)}</p>
    ${result}
    <button class="primary-button" data-apply-fix="${index}" ${done || busy ? 'disabled' : ''}>${label}</button>
  </div>`;
}

function renderTroubleshootSession(session) {
  setText('ts-session-device', `SESSION / ${session.device_label || session.device_id} / ${session.os_type || ''}`);
  setText('ts-session-issue', session.issue);
  const pill = $('ts-session-status');
  pill.className = `status-pill ${STATUS_TONES[session.status] || 'unknown'}`;
  pill.textContent = STATUS_LABELS[session.status] || session.status;

  $('ts-steps').innerHTML = (session.steps || []).map(renderStep).join('') || (tsPendingStep ? '' : '<li class="soft-label">Starting...</li>');
  if (tsPendingStep && !TERMINAL_STATUSES.includes(session.status)) $('ts-steps').appendChild(pendingStepElement(tsPendingStep));

  const report = session.report;
  const container = $('ts-report');
  if (session.status === 'failed' && !report) {
    container.innerHTML = `<p class="ts-error">${escapeHtml(session.error || 'The agent failed.')}</p>`;
    return;
  }
  if (!report) {
    container.innerHTML = '<p class="soft-label"><span class="spinner"></span> The agent is collecting evidence and reasoning. On a laptop CPU this can take a few minutes.</p>';
    return;
  }
  const evidence = (report.evidence || []).map((item) => `<li>${escapeHtml(item)}</li>`).join('');
  const manual = (report.manual_steps || []).map((item) => `<li>${escapeHtml(item)}</li>`).join('');
  const fixes = (report.proposed_fixes || []).map((fix, index) => renderFix(fix, index, session)).join('');
  const verify = session.verify ? `
    <div class="ts-verify ${session.verify.resolved ? 'ok' : session.verify.resolved === false ? 'failed' : ''}">
      <small>VERIFICATION AFTER ${escapeHtml(toolLabel(session.verify.after_fix))}</small>
      <strong>${session.verify.resolved ? 'Issue resolved' : session.verify.resolved === false ? 'Issue not resolved yet' : 'Not verified'}</strong>
      <p>${escapeHtml(session.verify.explanation)}</p>
      ${(session.verify.next_steps || []).length ? `<ul>${session.verify.next_steps.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul>` : ''}
    </div>` : '';
  container.innerHTML = `
    ${session.status === 'failed' && session.error ? `<p class="ts-error">${escapeHtml(session.error)}</p>` : ''}
    <div class="ts-root">
      <div class="ts-badges"><span class="severity severity-${escapeHtml(report.severity)}">${escapeHtml(report.severity)}</span><span class="soft-label">confidence: ${escapeHtml(report.confidence)}</span></div>
      <small>ROOT CAUSE</small>
      <strong>${escapeHtml(report.root_cause)}</strong>
    </div>
    ${evidence ? `<small class="ts-label">EVIDENCE</small><ul class="ts-list">${evidence}</ul>` : ''}
    ${verify}
    <small class="ts-label">PROPOSED FIXES</small>
    ${fixes || '<p class="soft-label">No automatic fix applies. Follow the manual steps below.</p>'}
    ${manual ? `<small class="ts-label">MANUAL STEPS</small><ol class="ts-list">${manual}</ol>` : ''}
  `;
}

async function applyTroubleshootFix(index) {
  const session = await fetch(`/api/troubleshoot/${tsSessionId}`).then((response) => response.json());
  const fix = session.report?.proposed_fixes?.[index];
  if (!fix) return;
  const warning = fix.risk === 'high' ? '\n\nHIGH RISK: this may need a reboot or take a long time.' : '';
  if (!confirm(`Apply "${toolLabel(fix.tool_id)}" on ${session.device_label}?\n\n${fix.description}${warning}`)) return;
  const response = await fetch(`/api/troubleshoot/${tsSessionId}/fixes/${index}/apply`, { method: 'POST' });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) alert(data.detail || 'Could not apply the fix');
  refreshTroubleshootSession();
}

function troubleshootDevice(deviceId) {
  switchTab('troubleshoot');
  loadTroubleshootTargets(deviceId);
  $('ts-issue').focus();
}

// ============================================================================
// Tab switching
// ============================================================================

const TAB_LOADERS = {
  troubleshoot: loadTroubleshoot,
  'add-agent': loadAddAgent,
  alerts: loadAlerts,
  agents: loadAgents,
  security: loadSecurity,
  performance: loadPerformance,
  logs: loadEventLogs,
};

function switchTab(tab) {
  document.querySelectorAll('.tab').forEach((element) => element.classList.toggle('active-tab', element.id === tab));
  document.querySelectorAll('.nav-item').forEach((element) => element.classList.toggle('active', element.dataset.tab === tab));
  if (location.hash.slice(1) !== tab) history.replaceState(null, '', `#${tab}`);
  const loader = TAB_LOADERS[tab];
  if (loader) loader();
}

// ============================================================================
// Event wiring
// ============================================================================

function wireEvents() {
  document.querySelectorAll('.nav-item').forEach((button) => button.addEventListener('click', () => switchTab(button.dataset.tab)));

  $('refresh-alerts').addEventListener('click', loadAlerts);
  $('alerts-body').addEventListener('click', (event) => {
    if (event.target.dataset.diagnose) diagnoseAlert(event.target.dataset.diagnose);
  });

  $('refresh-security').addEventListener('click', loadSecurity);
  $('run-performance').addEventListener('click', runPerformanceTest);
  $('refresh-logs').addEventListener('click', loadEventLogs);
  $('diagnose-logs-button').addEventListener('click', diagnoseEventLogs);

  $('chat-form').addEventListener('submit', sendChat);

  $('add-agent-button').addEventListener('click', () => switchTab('add-agent'));
  $('generate-token-button').addEventListener('click', generateToken);
  $('download-agent-button').addEventListener('click', downloadAgent);
  $('copy-install').addEventListener('click', copyInstallCommand);
  $('enroll-server').addEventListener('input', updateInstallInstructions);

  $('ts-start').addEventListener('click', startTroubleshoot);
  $('ts-refresh-history').addEventListener('click', loadTroubleshootHistory);
  $('ts-playbooks').addEventListener('click', (event) => {
    const chip = event.target.closest('[data-playbook]');
    if (chip) selectPlaybook(chip.dataset.playbook);
  });
  $('ts-history').addEventListener('click', (event) => {
    const item = event.target.closest('[data-session]');
    if (item) openTroubleshootSession(item.dataset.session);
  });
  $('ts-report').addEventListener('click', (event) => {
    const button = event.target.closest('[data-apply-fix]');
    if (button) applyTroubleshootFix(Number(button.dataset.applyFix));
  });
  $('ts-issue').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) startTroubleshoot();
  });

  document.querySelectorAll('.platform-tab').forEach((button) => button.addEventListener('click', () => {
    selectedPlatform = button.dataset.platform;
    document.querySelectorAll('.platform-tab').forEach((item) => item.classList.toggle('active', item === button));
    updateInstallInstructions();
  }));

  $('agents-grid').addEventListener('click', (event) => {
    const card = event.target.closest('[data-device]');
    if (card) loadDevice(card.dataset.device);
  });
  $('device-detail').addEventListener('click', (event) => {
    if (event.target.dataset.deviceDiagnose) diagnoseDevice(event.target.dataset.deviceDiagnose);
    if (event.target.dataset.deviceTroubleshoot) troubleshootDevice(event.target.dataset.deviceTroubleshoot);
  });
}

// ============================================================================
// Bootstrap
// ============================================================================

wireEvents();
connectMonitor();
loadHealth();
loadAlerts();
checkAI();
setInterval(loadHealth, 5000);
setInterval(checkAI, 30000);
if (location.hash && $(location.hash.slice(1))?.classList.contains('tab')) switchTab(location.hash.slice(1));
