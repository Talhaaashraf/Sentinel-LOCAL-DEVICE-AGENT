const $ = (id) => document.getElementById(id);
let latestReport = null;
let socket = null;
let reconnectTimer = null;
let chatHistory = [];

function setText(id, value) { $(id).textContent = value; }
function percent(value) { return typeof value === 'number' ? `${value.toFixed(1)}%` : '--'; }
function level(value, warning = 75, critical = 90) { return value >= critical ? 'critical' : value >= warning ? 'warning' : 'ok'; }

function renderReport(report) {
  latestReport = report;
  const cpu = report.cpu || {}, ram = report.memory?.ram || {}, disks = report.disk || [], network = report.network || {}, battery = report.battery;
  const diskValue = disks.reduce((max, disk) => Math.max(max, disk.usage_percent || 0), 0);
  const cpuLevel = level(cpu.total_usage_percent || 0, 75, 85), ramLevel = level(ram.usage_percent || 0, 75, 85), diskLevel = level(diskValue, 75, 90);
  [['cpu', cpu.total_usage_percent, cpuLevel], ['ram', ram.usage_percent, ramLevel], ['disk', diskValue, diskLevel]].forEach(([name, value, state]) => { $(`${name}-gauge`).closest('.metric-card').className = `metric-card ${state}`; setText(`${name}-value`, typeof value === 'number' ? value.toFixed(1) : '--'); setText(`${name}-state`, state.toUpperCase()); });
  setText('cpu-detail', `${cpu.logical_cores || '--'} logical cores / ${cpu.current_frequency_mhz || '--'} MHz`);
  setText('ram-detail', `${ram.available || '--'} available`);
  setText('disk-detail', disks.length ? `${disks.length} mounted volume${disks.length === 1 ? '' : 's'}` : 'No volumes');
  setText('network-value', network.interfaces ? `${network.interfaces.filter((item) => item.status === 'UP').length} up` : '--'); setText('network-state', 'LIVE'); setText('network-detail', `${network.bytes_received || '--'} received`);
  setText('battery-value', battery ? percent(battery.percent) : 'N/A'); setText('battery-state', battery ? (battery.charging ? 'CHARGING' : 'ON BATTERY') : 'N/A'); setText('battery-detail', battery ? battery.time_remaining : 'No battery detected');
  setText('hostname', report.system?.hostname || '--'); setText('os-name', `${report.system?.os_name || '--'} ${report.system?.os_release || ''}`); setText('uptime', report.system?.uptime || '--'); setText('architecture', report.system?.architecture || '--'); setText('process-count', `${report.processes_count ?? '--'} processes`); setText('last-updated', `Updated ${new Date().toLocaleTimeString()}`);
  setText('chat-host', report.system?.hostname || '--'); setText('chat-cpu', percent(cpu.total_usage_percent)); setText('chat-ram', percent(ram.usage_percent)); setText('chat-disk', percent(diskValue));
}

function renderHealth(health) { const banner = $('health-banner'); banner.className = `health-banner ${health.status}`; setText('health-title', health.status === 'ok' ? 'System healthy' : `${health.status} signals detected`); setText('health-message', health.message); }
function setConnection(state, detail) { $('connection-state').className = `connection-state ${state}`; setText('connection-label', state === 'connected' ? 'Monitor connected' : state === 'disconnected' ? 'Disconnected' : 'Connecting'); setText('connection-detail', detail); }

function connectMonitor() {
  clearTimeout(reconnectTimer); setConnection('', 'Opening monitor stream');
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws'; socket = new WebSocket(`${protocol}://${location.host}/ws/monitor`);
  socket.onopen = () => setConnection('connected', 'Live stream / 5 second interval');
  socket.onmessage = (event) => renderReport(JSON.parse(event.data));
  socket.onclose = () => { setConnection('disconnected', 'Retrying in 3 seconds'); reconnectTimer = setTimeout(connectMonitor, 3000); };
  socket.onerror = () => socket.close();
}

async function loadHealth() { try { const response = await fetch('/api/diagnostics/health'); if (!response.ok) throw new Error('Health unavailable'); renderHealth(await response.json()); } catch (error) { renderHealth({ status: 'critical', message: error.message }); } }
async function loadAlerts() { const body = $('alerts-body'); try { const response = await fetch('/api/alerts'); if (!response.ok) throw new Error('Alert history unavailable'); const data = await response.json(); setText('alert-count', data.alerts.length); if (!data.alerts.length) { body.innerHTML = '<tr><td colspan="5" class="empty-state">No alerts recorded. The local rules engine is watching.</td></tr>'; return; } body.innerHTML = data.alerts.map((alert) => `<tr><td>${new Date(alert.timestamp).toLocaleString()}</td><td><span class="severity severity-${alert.severity}">${alert.severity}</span></td><td>${alert.category}</td><td>${alert.description}</td><td><button class="diagnose-button" data-diagnose="${alert.id}">Diagnose with AI</button></td></tr><tr id="diagnosis-${alert.id}" class="diagnosis-row hidden"><td colspan="5"><div class="diagnosis" id="diagnosis-content-${alert.id}"></div></td></tr>`).join(''); } catch (error) { body.innerHTML = `<tr><td colspan="5" class="empty-state">${error.message}</td></tr>`; } }

function renderDiagnosis(container, data) { container.replaceChildren(); const summary = document.createElement('strong'); summary.textContent = data.summary || data.plain_explanation || 'No summary returned.'; container.appendChild(summary); const root = document.createElement('p'); root.textContent = `Likely root cause: ${data.root_cause || data.technical_root_cause || 'Unknown'}`; container.appendChild(root); const list = document.createElement('ul'); (data.steps || data.recommendations || []).forEach((step) => { const item = document.createElement('li'); item.textContent = String(step); list.appendChild(item); }); container.appendChild(list); }
async function diagnoseAlert(id) { const row = $(`diagnosis-${id}`), content = $(`diagnosis-content-${id}`); row.classList.remove('hidden'); content.textContent = 'Consulting technician...'; try { const response = await fetch('/api/agent/diagnose', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ diagnostics: latestReport, alert_history: [] }) }); const data = await response.json(); if (!response.ok) throw new Error(data.detail || 'AI unavailable'); renderDiagnosis(content, data); } catch (error) { content.textContent = error.message; } }

async function checkAI() { try { const response = await fetch('/api/agent/chat'); const status = await response.json(); if (!status.available) $('ai-banner').classList.remove('hidden'); } catch (_) { $('ai-banner').classList.remove('hidden'); } }
function addChatMessage(text, role) { const message = document.createElement('div'); message.className = `chat-message ${role}`; if (role === 'assistant' && text && typeof text === 'object') { renderDiagnosis(message, text); } else { message.textContent = String(text); } $('chat-messages').appendChild(message); $('chat-messages').scrollTop = $('chat-messages').scrollHeight; }
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
      body: JSON.stringify({ message, diagnostics: latestReport, conversation_history: chatHistory })
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

let generatedToken = '';
let selectedPlatform = 'windows';
function switchTab(tab) { document.querySelectorAll('.tab').forEach((element) => element.classList.toggle('active-tab', element.id === tab)); document.querySelectorAll('.nav-item').forEach((element) => element.classList.toggle('active', element.dataset.tab === tab)); if (tab === 'alerts') loadAlerts(); if (tab === 'agents') loadAgents(); }
async function loadAgents() { const grid = $('agents-grid'); try { const response = await fetch('/api/agents'); if (!response.ok) throw new Error('Agent list unavailable'); const data = await response.json(); setText('agent-count', data.agents.length); if (!data.agents.length) { grid.innerHTML = '<div class="empty-state">No devices connected yet - download the agent to get started.</div>'; return; } grid.innerHTML = data.agents.map((agent) => `<button class="agent-card" data-device="${agent.device_id}"><div class="agent-card-top"><span class="os-badge">${agent.os_type === 'Windows' ? 'W' : agent.os_type === 'Darwin' ? 'M' : 'L'}</span><span class="device-status ${agent.status}">${agent.status}</span></div><strong>${agent.nickname}</strong><small>${agent.hostname} / ${agent.os_type}</small><div class="agent-card-foot"><span>${new Date(agent.last_seen).toLocaleString()}</span><b>${agent.health.status}</b></div></button>`).join(''); } catch (error) { grid.innerHTML = `<div class="empty-state">${error.message}</div>`; } }
async function loadDevice(deviceId) { const detail = $('device-detail'); detail.classList.remove('hidden'); detail.innerHTML = '<p>Loading device snapshot...</p>'; try { const [snapshot, alerts] = await Promise.all([fetch(`/api/agents/${deviceId}/diagnostics`).then((response) => response.json()), fetch(`/api/agents/${deviceId}/alerts`).then((response) => response.json())]); const report = snapshot.diagnostics; const disk = (report.disk || []).reduce((max, item) => Math.max(max, item.usage_percent || 0), 0); detail.innerHTML = `<div class="section-heading"><div><p class="kicker">SELECTED ENDPOINT</p><h2>${report.system?.hostname || deviceId}</h2></div><button class="ghost-button" data-device-diagnose="${deviceId}">Diagnose with AI</button></div><div class="mini-metrics"><div><small>CPU</small><strong>${percent(report.cpu?.total_usage_percent)}</strong></div><div><small>RAM</small><strong>${percent(report.memory?.ram?.usage_percent)}</strong></div><div><small>DISK</small><strong>${percent(disk)}</strong></div><div><small>ALERTS</small><strong>${alerts.alerts.length}</strong></div></div><div id="device-diagnosis" class="diagnosis"></div>`; } catch (error) { detail.innerHTML = `<p>${error.message}</p>`; } }
async function generateToken() { const result = $('token-result'); try { const response = await fetch('/api/agents/generate-token', { method: 'POST', headers: { 'X-Admin-Key': $('admin-key-input').value } }); const data = await response.json(); if (!response.ok) throw new Error(data.detail || 'Admin authentication failed'); generatedToken = data.token; result.textContent = `${generatedToken} (expires ${new Date(data.expires_at).toLocaleString()})`; result.classList.remove('hidden'); $('download-agent-button').disabled = false; updateInstallInstructions(); } catch (error) { result.textContent = error.message; result.classList.remove('hidden'); } }
function updateInstallInstructions() { $('install-instructions').textContent = generatedToken ? `${selectedPlatform === 'windows' ? 'DeviceHealthAgent.exe' : 'DeviceHealthAgent'}: place agent_config.json beside it (built from agent_config.example.json) with server_url=http://127.0.0.1:8000 and token=${generatedToken}. Fallback: pip install -r agent_requirements.txt && python monitor_agent.py` : 'Generate a token to show install instructions.'; }
function openAgentModal() { $('agent-modal').classList.remove('hidden'); }
function closeAgentModal() { $('agent-modal').classList.add('hidden'); }
async function diagnoseDevice(deviceId) { const output = $('device-diagnosis'); output.textContent = 'Consulting technician...'; try { const response = await fetch(`/api/agents/${deviceId}/agent-diagnose`, { method: 'POST' }); const data = await response.json(); if (!response.ok) throw new Error(data.detail || 'AI unavailable'); renderDiagnosis(output, data); } catch (error) { output.textContent = error.message; } }
document.querySelectorAll('.nav-item').forEach((button) => button.addEventListener('click', () => switchTab(button.dataset.tab)));
$('refresh-alerts').addEventListener('click', loadAlerts); $('alerts-body').addEventListener('click', (event) => { if (event.target.dataset.diagnose) diagnoseAlert(event.target.dataset.diagnose); }); $('chat-form').addEventListener('submit', sendChat);
document.querySelectorAll('.nav-item').forEach((button) => button.addEventListener('click', () => switchTab(button.dataset.tab))); $('add-agent-button').addEventListener('click', openAgentModal); $('close-agent-modal').addEventListener('click', closeAgentModal); $('generate-token-button').addEventListener('click', generateToken); document.querySelectorAll('.platform-tab').forEach((button) => button.addEventListener('click', () => { selectedPlatform = button.dataset.platform; document.querySelectorAll('.platform-tab').forEach((item) => item.classList.toggle('active', item === button)); updateInstallInstructions(); })); $('download-agent-button').addEventListener('click', async () => { const result = $('install-instructions'); result.textContent = 'Checking for a built installer...'; try { const response = await fetch(`/api/agents/download/${selectedPlatform}`); const contentType = response.headers.get('content-type') || ''; if (!response.ok || contentType.includes('application/json')) { const data = await response.json(); result.textContent = `${data.message}\n\nManual fallback:\n${data.manual_install}`; return; } const blob = await response.blob(); const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = selectedPlatform === 'windows' ? 'DeviceHealthAgent.exe' : 'DeviceHealthAgent'; document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); updateInstallInstructions(); } catch (error) { console.error('Agent download failed:', error); result.textContent = `Download failed: ${error.message}\n\nManual fallback: pip install -r agent_requirements.txt && python monitor_agent.py`; } }); $('agents-grid').addEventListener('click', (event) => { const card = event.target.closest('[data-device]'); if (card) loadDevice(card.dataset.device); }); $('device-detail').addEventListener('click', (event) => { if (event.target.dataset.deviceDiagnose) diagnoseDevice(event.target.dataset.deviceDiagnose); });
connectMonitor(); loadHealth(); loadAlerts(); checkAI(); setInterval(loadHealth, 5000);
