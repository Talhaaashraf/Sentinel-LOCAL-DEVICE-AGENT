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
    const response = await fetch('/api/agent/chat');
    const status = await response.json();
    if (!status.available) $('ai-banner').classList.remove('hidden');
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
        <button class="ghost-button" data-device-diagnose="${deviceId}">Diagnose with AI</button>
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
    result.textContent = `Token: ${generatedToken} (expires ${new Date(data.expires_at).toLocaleString()})`;
    result.classList.remove('hidden');
    updateInstallInstructions();
  } catch (error) {
    result.textContent = error.message;
    result.classList.remove('hidden');
  }
}

function installCommand() {
  const origin = location.origin;
  const persist = $('install-persist') && $('install-persist').checked ? '&persist=1' : '';
  if (selectedPlatform === 'windows') {
    return `irm "${origin}/install.ps1?t=${generatedToken}${persist}" | iex`;
  }
  return `curl -fsSL "${origin}/install.sh?t=${generatedToken}${persist}" | sh`;
}

function updateInstallInstructions() {
  const target = $('install-oneliner');
  if (!target) return;
  if (!generatedToken) {
    target.textContent = 'Generate a token to show the install command.';
    if ($('copy-install')) $('copy-install').disabled = true;
    return;
  }
  target.textContent = installCommand();
  if ($('copy-install')) $('copy-install').disabled = false;
}

function openAgentModal() {
  $('agent-modal').classList.remove('hidden');
}

function closeAgentModal() {
  $('agent-modal').classList.add('hidden');
}

async function downloadAgent() {
  const result = $('install-instructions');
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
    updateInstallInstructions();
  } catch (error) {
    console.error('Agent download failed:', error);
    result.textContent = `Download failed: ${error.message}\n\nManual fallback: pip install -r agent_requirements.txt && python monitor_agent.py`;
  }
}

// ============================================================================
// Tab switching
// ============================================================================

const TAB_LOADERS = {
  alerts: loadAlerts,
  agents: loadAgents,
  security: loadSecurity,
  performance: loadPerformance,
  logs: loadEventLogs,
};

// Exposed so diagnose.js (a separate classic script) can register its own tab
// loaders and reuse the install-command renderer.
window.TAB_LOADERS = TAB_LOADERS;
window.updateInstallInstructions = updateInstallInstructions;

function switchTab(tab) {
  document.querySelectorAll('.tab').forEach((element) => element.classList.toggle('active-tab', element.id === tab));
  document.querySelectorAll('.nav-item').forEach((element) => element.classList.toggle('active', element.dataset.tab === tab));
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

  $('add-agent-button').addEventListener('click', openAgentModal);
  $('close-agent-modal').addEventListener('click', closeAgentModal);
  $('generate-token-button').addEventListener('click', generateToken);
  $('download-agent-button').addEventListener('click', downloadAgent);

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
