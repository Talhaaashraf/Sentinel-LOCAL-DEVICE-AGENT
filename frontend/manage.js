// Sentinel — Software (apps / startup / backups) and Automation (schedules / fleet) tabs.
// Loaded after app.js and diagnose.js; registers its loaders on window.TAB_LOADERS.

(function () {
  const api = async (path, options) => {
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
    return data;
  };
  const el = (id) => document.getElementById(id);
  const esc = (t) => String(t == null ? '' : t).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  let devices = [];
  let apps = [];

  async function fetchDevices() {
    try { devices = (await api('/api/agents')).agents || []; } catch (_e) { devices = []; }
    return devices;
  }

  function fillDeviceSelect(select, onlineOnly) {
    if (!select) return;
    const list = onlineOnly ? devices.filter((d) => d.status === 'online') : devices;
    const current = select.value;
    select.innerHTML = list.length
      ? list.map((d) => `<option value="${d.device_id}">${esc(d.nickname)} — ${esc(d.os_type)}${d.status === 'online' ? '' : ' (offline)'}</option>`).join('')
      : '<option value="">No online devices</option>';
    if (current) select.value = current;
  }

  // ----- generic: create a pre-approved command and poll it -----
  async function runCommand(deviceId, tool, args, outputEl) {
    outputEl.classList.remove('hidden');
    outputEl.textContent = `Running ${tool}…`;
    try {
      const command = await api(`/api/devices/${deviceId}/commands`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool, args, approve: true }),
      });
      for (let i = 0; i < 90; i++) {
        const c = await api(`/api/commands/${command.id}`);
        if (['done', 'error', 'cancelled', 'denied', 'rejected'].includes(c.status)) {
          outputEl.textContent = `${tool} — ${c.status}\n` + (c.result ? JSON.stringify(c.result, null, 2) : (c.error || ''));
          return c;
        }
        if (c.progress) outputEl.textContent = `${tool} — running…\n` + JSON.stringify(c.progress.progress || c.progress, null, 2);
        await new Promise((r) => setTimeout(r, 1500));
      }
      outputEl.textContent = `${tool} — timed out waiting for the agent.`;
    } catch (error) { outputEl.textContent = error.message; }
  }

  // ========================================================= SOFTWARE: apps
  function badges(app) {
    const out = [];
    if (app.orphaned) out.push(`<span class="badge badge-bad" title="${esc((app.orphan_reasons || []).join(', '))}">broken / orphaned</span>`);
    if (app.hidden_from_control_panel) out.push('<span class="badge badge-warn">hidden</span>');
    if (app.source === 'store') out.push('<span class="badge">store</span>');
    return out.join(' ') || '<span class="soft-label">—</span>';
  }

  function appActions(app) {
    const buttons = [`<button class="mini-btn" data-act="uninstall" data-id="${esc(app.id)}">Uninstall</button>`,
                     `<button class="mini-btn warn" data-act="force" data-id="${esc(app.id)}">Force remove</button>`];
    if (app.orphaned && app.source === 'registry') {
      buttons.push(`<button class="mini-btn" data-act="entry" data-id="${esc(app.id)}">Remove entry</button>`);
    }
    buttons.push(`<button class="mini-btn" data-act="leftovers" data-name="${esc(app.name)}">Leftovers</button>`);
    return buttons.join(' ');
  }

  function renderApps() {
    const problemOnly = el('sw-problem-only').checked;
    const rows = apps.filter((a) => !problemOnly || a.orphaned || a.hidden_from_control_panel);
    el('sw-apps-body').innerHTML = rows.length
      ? rows.map((a) => `<tr>
          <td>${esc(a.name)}</td><td>${esc(a.version || '')}</td><td>${esc(a.publisher || '')}</td>
          <td>${esc(a.estimated_size || '')}</td><td>${badges(a)}</td><td class="row-actions">${appActions(a)}</td>
        </tr>`).join('')
      : '<tr><td colspan="6" class="empty-state">No matching apps.</td></tr>';
  }

  async function loadApps() {
    const deviceId = el('sw-device').value;
    if (!deviceId) { alert('Pick a laptop first.'); return; }
    el('sw-summary').textContent = 'Asking the agent for installed apps…';
    try {
      const data = await api(`/api/devices/${deviceId}/software?search=${encodeURIComponent(el('sw-search').value.trim())}`);
      apps = data.apps || [];
      el('sw-summary').textContent = `${data.count} apps — ${data.hidden_count} hidden, ${data.orphaned_count} broken/orphaned.`;
      renderApps();
    } catch (error) { el('sw-summary').textContent = error.message; }
  }

  async function appAction(act, id, name) {
    const deviceId = el('sw-device').value;
    const app = apps.find((a) => a.id === id);
    const label = app ? app.name : name;
    const prompts = {
      uninstall: `Uninstall "${label}"?`,
      force: `FORCE-REMOVE "${label}"? Its processes will be killed and its folder quarantined (restorable from Backups).`,
      entry: `Remove the broken Control Panel entry for "${label}"? (backed up first)`,
    };
    if (act !== 'leftovers' && !confirm(prompts[act])) return;
    const output = el('sw-output');
    if (act === 'uninstall') await runCommand(deviceId, 'uninstall_app', { app_id: id, mode: 'standard' }, output);
    else if (act === 'force') await runCommand(deviceId, 'uninstall_app', { app_id: id, mode: 'force' }, output);
    else if (act === 'entry') await runCommand(deviceId, 'remove_app_entry', { app_id: id }, output);
    else if (act === 'leftovers') await runCommand(deviceId, 'uninstall_leftovers', { name }, output);
    if (act !== 'leftovers') loadApps();
  }

  // ========================================================= SOFTWARE: startup
  async function loadStartup() {
    const deviceId = el('sw-device').value;
    if (!deviceId) return;
    el('sw-startup-summary').textContent = 'Loading startup items…';
    try {
      const data = await api(`/api/devices/${deviceId}/startup`);
      const items = data.items || [];
      el('sw-startup-summary').textContent = `${items.length} startup items.`;
      el('sw-startup-body').innerHTML = items.length
        ? items.map((i) => `<tr><td>${esc(i.name)}</td><td><code>${esc((i.command || '').slice(0, 80))}</code></td>
            <td>${esc(i.location || '')}</td>
            <td><button class="mini-btn warn" data-startup="${esc(i.id)}">Disable</button></td></tr>`).join('')
        : '<tr><td colspan="4" class="empty-state">No startup items.</td></tr>';
    } catch (error) { el('sw-startup-summary').textContent = error.message; }
  }

  async function disableStartup(itemId) {
    if (!confirm('Disable this startup item? It is backed up and can be re-enabled from Backups.')) return;
    await runCommand(el('sw-device').value, 'disable_startup_item', { item_id: itemId }, el('sw-output'));
    loadStartup();
  }

  // ========================================================= SOFTWARE: backups
  async function loadBackups() {
    const deviceId = el('sw-device').value;
    if (!deviceId) return;
    try {
      const data = await api(`/api/devices/${deviceId}/backups`);
      const rows = data.backups || [];
      el('sw-backups-body').innerHTML = rows.length
        ? rows.map((b) => `<tr>
            <td>${esc(b.created_at)}</td><td>${esc(b.type)}</td><td>${esc(b.app || b.original || '')}</td>
            <td>${b.restored ? '<span class="badge">restored</span>' : (b.available ? '<span class="badge badge-ok">available</span>' : '<span class="badge badge-warn">missing</span>')}</td>
            <td>${(!b.restored && b.available) ? `<button class="mini-btn" data-restore="${esc(b.id)}">Restore</button>` : '—'}</td>
          </tr>`).join('')
        : '<tr><td colspan="5" class="empty-state">No backups recorded yet.</td></tr>';
    } catch (error) { el('sw-backups-body').innerHTML = `<tr><td colspan="5" class="empty-state">${esc(error.message)}</td></tr>`; }
  }

  async function restore(entryId) {
    if (!confirm('Restore this item to its original location?')) return;
    await runCommand(el('sw-device').value, 'restore_backup', { entry_id: entryId }, el('sw-output'));
    loadBackups();
  }

  async function initSoftware() {
    await fetchDevices();
    fillDeviceSelect(el('sw-device'), true);
  }

  // ========================================================= PROCESSES (live)
  let procTimer = null;
  let procLoading = false;

  async function initProcesses() {
    await fetchDevices();
    fillDeviceSelect(el('proc-device'), true);
    loadProcesses();
    scheduleProcRefresh();
  }

  function scheduleProcRefresh() {
    clearTimeout(procTimer);
    const active = document.getElementById('processes').classList.contains('active-tab');
    const auto = el('proc-auto') && el('proc-auto').checked;
    const onProcsSub = !el('proc-procs').classList.contains('hidden');
    if (active && auto && onProcsSub) procTimer = setTimeout(() => { loadProcesses(true); }, 5000);
  }

  async function loadProcesses(quiet) {
    const deviceId = el('proc-device').value;
    if (!deviceId) { el('proc-body').innerHTML = '<tr><td colspan="6" class="empty-state">No online device.</td></tr>'; return; }
    if (procLoading) return;           // avoid overlapping samples racing the agent
    procLoading = true;
    if (!quiet) el('proc-summary').textContent = 'Loading…';
    try {
      const data = await api(`/api/devices/${deviceId}/processes?sort=${el('proc-sort').value}`);
      const procs = data.processes || [];
      el('proc-summary').textContent = `CPU ${data.total_cpu_percent ?? '?'}% · RAM ${data.ram_used_percent ?? '?'}% · ${data.process_count} processes · updated ${new Date().toLocaleTimeString()}`;
      el('proc-body').innerHTML = procs.length
        ? procs.map((p) => `<tr>
            <td>${p.pid}</td><td>${esc(p.name)}</td><td>${p.cpu_percent}</td><td>${esc(p.memory)}</td><td>${esc(p.user || '')}</td>
            <td><button class="mini-btn warn" data-kill="${p.pid}" data-name="${esc(p.name)}">End</button></td></tr>`).join('')
        : '<tr><td colspan="6" class="empty-state">No processes returned.</td></tr>';
    } catch (error) { el('proc-summary').textContent = error.message; }
    finally { procLoading = false; }
    scheduleProcRefresh();
  }

  async function killProcess(pid, name) {
    if (!confirm(`End process ${name} (PID ${pid})? Unsaved work in that app will be lost.`)) return;
    await runCommand(el('proc-device').value, 'kill_process', { pid: Number(pid) }, el('proc-output'));
    loadProcesses();
  }

  async function loadServices() {
    const deviceId = el('proc-device').value;
    if (!deviceId) return;
    el('svc-summary').textContent = 'Loading…';
    try {
      const data = await api(`/api/devices/${deviceId}/services?search=${encodeURIComponent(el('svc-search').value.trim())}`);
      const svcs = data.services || [];
      el('svc-summary').textContent = `${data.count} services`;
      el('svc-body').innerHTML = svcs.length
        ? svcs.slice(0, 300).map((s) => {
            const name = s.Name || s.name || '';
            const state = s.State || s.Active || s.State || '';
            return `<tr><td>${esc(name)}</td><td>${esc(s.DisplayName || '')}</td><td>${esc(state)}</td>
              <td><button class="mini-btn" data-svc="${esc(name)}" data-act="restart">Restart</button>
                  <button class="mini-btn warn" data-svc="${esc(name)}" data-act="stop">Stop</button>
                  <button class="mini-btn" data-svc="${esc(name)}" data-act="start">Start</button></td></tr>`;
          }).join('')
        : '<tr><td colspan="4" class="empty-state">No services.</td></tr>';
    } catch (error) { el('svc-summary').textContent = error.message; }
  }

  async function serviceAction(name, action) {
    if (!confirm(`${action} service "${name}"?`)) return;
    await runCommand(el('proc-device').value, 'service_control', { name, action }, el('proc-output'));
    loadServices();
  }

  // ========================================================= HISTORY & TRENDS
  function lineChart(containerId, values, color, { max = 100, unit = "%" } = {}) {
    const el_ = el(containerId);
    const pts = values.filter((v) => v != null);
    if (!pts.length) { el_.innerHTML = '<div class="chart-empty">no data yet</div>'; return; }
    const W = 320, H = 90, pad = 4;
    const top = Math.max(max, Math.ceil(Math.max(...pts) / 10) * 10) || 100;
    const n = values.length;
    const x = (i) => pad + (i / Math.max(1, n - 1)) * (W - 2 * pad);
    const y = (v) => H - pad - (v / top) * (H - 2 * pad);
    let d = "", started = false;
    values.forEach((v, i) => {
      if (v == null) { started = false; return; }
      d += `${started ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)} `;
      started = true;
    });
    const last = pts[pts.length - 1];
    const area = d ? `${d}L${x(n - 1).toFixed(1)},${H - pad} L${x(0).toFixed(1)},${H - pad} Z` : "";
    el_.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="spark">
      <path d="${area}" fill="${color}" opacity="0.12"/>
      <path d="${d}" fill="none" stroke="${color}" stroke-width="1.6"/>
    </svg>`;
    return last;
  }

  async function initHistory() {
    await fetchDevices();
    fillDeviceSelect(el('hist-device'), false);
    loadHistory();
  }

  async function loadHistory() {
    const deviceId = el('hist-device').value;
    if (!deviceId) { el('hist-summary').textContent = 'No device selected.'; return; }
    const hours = el('hist-range').value;
    el('hist-summary').textContent = 'Loading…';
    try {
      const data = await api(`/api/devices/${deviceId}/history?hours=${hours}`);
      el('hist-summary').textContent = data.points ? `${data.points} samples over the last ${hours}h.` : 'No samples in this window yet — history builds up as the agent reports.';
      const s = data.series;
      const nums = (a) => a.filter((v) => v != null);
      const last = (a) => { const f = nums(a); return f.length ? f[f.length - 1] : null; };
      lineChart('chart-cpu', s.cpu, '#72dfc0');
      lineChart('chart-ram', s.ram, '#82b8ff');
      lineChart('chart-disk', s.disk, '#f2c66d');
      lineChart('chart-health', s.health, '#8ad18a', { max: 100 });
      el('hist-cpu-now').textContent = last(s.cpu) != null ? last(s.cpu) + '%' : '--';
      el('hist-ram-now').textContent = last(s.ram) != null ? last(s.ram) + '%' : '--';
      el('hist-disk-now').textContent = last(s.disk) != null ? last(s.disk) + '%' : '--';
      el('hist-health-now').textContent = last(s.health) != null ? last(s.health) : '--';
    } catch (error) { el('hist-summary').textContent = error.message; }
    try {
      const runs = (await api(`/api/devices/${deviceId}/stress-history`)).runs || [];
      el('hist-stress-body').innerHTML = runs.length
        ? runs.map((r) => `<tr><td>${new Date(r.when).toLocaleString()}</td><td>${esc(r.test)}</td><td>${esc(r.summary || '')}</td></tr>`).join('')
        : '<tr><td colspan="3" class="empty-state">No stress runs recorded yet.</td></tr>';
    } catch (_e) { /* ignore */ }
  }

  // ========================================================= AUTOMATION: schedules
  async function initAutomation() {
    await fetchDevices();
    fillDeviceSelect(el('sched-device'), true);
    try {
      const presets = (await api('/api/presets')).presets || [];
      el('sched-preset').innerHTML = presets.map((p) => `<option value="${p.id}">${esc(p.label)}</option>`).join('');
    } catch (_e) { /* ignore */ }
    loadSchedules();
    renderFleetDevices();
  }

  async function loadSchedules() {
    try {
      const data = await api('/api/schedules');
      const byId = Object.fromEntries(devices.map((d) => [d.device_id, d.nickname]));
      el('sched-body').innerHTML = data.schedules.length
        ? data.schedules.map((s) => `<tr>
            <td>${esc(byId[s.device_id] || s.device_id)}</td><td>${esc(s.preset)}</td><td>${s.interval_minutes} min</td>
            <td>${s.last_run ? new Date(s.last_run).toLocaleString() : 'never'}</td>
            <td>${esc(s.last_result || '')}</td>
            <td><button class="mini-btn" data-sched-toggle="${s.id}" data-enabled="${s.enabled}">${s.enabled ? 'Pause' : 'Resume'}</button>
                <button class="mini-btn warn" data-sched-del="${s.id}">Delete</button></td>
          </tr>`).join('')
        : '<tr><td colspan="6" class="empty-state">No schedules yet.</td></tr>';
    } catch (error) { el('sched-body').innerHTML = `<tr><td colspan="6" class="empty-state">${esc(error.message)}</td></tr>`; }
  }

  async function addSchedule() {
    const deviceId = el('sched-device').value;
    if (!deviceId) { alert('Pick a laptop.'); return; }
    try {
      await api('/api/schedules', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: deviceId, preset: el('sched-preset').value, interval_minutes: Number(el('sched-interval').value) || 60 }),
      });
      loadSchedules();
    } catch (error) { alert(error.message); }
  }

  // ========================================================= AUTOMATION: fleet
  function renderFleetDevices() {
    const online = devices.filter((d) => d.status === 'online');
    el('fleet-devices').innerHTML = online.length
      ? online.map((d) => `<label class="fleet-item"><input type="checkbox" value="${d.device_id}"> ${esc(d.nickname)} <small>${esc(d.os_type)}</small></label>`).join('')
      : '<p class="soft-label">No online devices.</p>';
  }

  async function runFleet() {
    const ids = Array.from(document.querySelectorAll('#fleet-devices input:checked')).map((i) => i.value);
    if (!ids.length) { alert('Select at least one laptop.'); return; }
    const tool = el('fleet-tool').value;
    if (!confirm(`Run "${tool}" on ${ids.length} laptop(s)?`)) return;
    const output = el('fleet-output');
    output.classList.remove('hidden');
    output.textContent = 'Dispatching…';
    try {
      const data = await api('/api/fleet/commands', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_ids: ids, tool, args: {}, approve: true }),
      });
      output.textContent = `Dispatched to ${data.created.length} laptop(s).` + (data.skipped.length ? `\nSkipped: ${JSON.stringify(data.skipped)}` : '')
        + '\n' + data.created.map((c) => `${c.nickname}: ${c.command_id}`).join('\n');
    } catch (error) { output.textContent = error.message; }
  }

  // ========================================================= sub-tab switching
  function wireSubTabs(sectionId, panels) {
    document.querySelectorAll(`#${sectionId} .sub-tab`).forEach((b) => b.addEventListener('click', () => {
      document.querySelectorAll(`#${sectionId} .sub-tab`).forEach((x) => x.classList.toggle('active', x === b));
      Object.entries(panels).forEach(([sub, id]) => el(id).classList.toggle('hidden', sub !== b.dataset.sub));
      if (b.dataset.sub === 'startup') loadStartup();
      if (b.dataset.sub === 'backups') loadBackups();
      if (b.dataset.sub === 'fleet') renderFleetDevices();
      if (b.dataset.sub === 'svcs') loadServices();
      if (b.dataset.sub === 'procs') scheduleProcRefresh();
    }));
  }

  function wire() {
    if (window.TAB_LOADERS) {
      window.TAB_LOADERS.software = initSoftware;
      window.TAB_LOADERS.automation = initAutomation;
      window.TAB_LOADERS.history = initHistory;
      window.TAB_LOADERS.processes = initProcesses;
    }
    wireSubTabs('software', { apps: 'sw-apps', startup: 'sw-startup', backups: 'sw-backups' });
    wireSubTabs('automation', { sched: 'auto-sched', fleet: 'auto-fleet' });
    wireSubTabs('processes', { procs: 'proc-procs', svcs: 'proc-svcs' });

    el('sw-refresh').addEventListener('click', loadApps);
    el('sw-search').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadApps(); });
    el('sw-problem-only').addEventListener('change', renderApps);
    el('sw-device').addEventListener('change', () => { apps = []; el('sw-apps-body').innerHTML = '<tr><td colspan="6" class="empty-state">Click Load apps.</td></tr>'; });
    el('sw-apps-body').addEventListener('click', (e) => {
      const b = e.target.closest('[data-act]');
      if (b) appAction(b.dataset.act, b.dataset.id, b.dataset.name);
    });
    el('sw-startup-body').addEventListener('click', (e) => {
      const b = e.target.closest('[data-startup]');
      if (b) disableStartup(b.dataset.startup);
    });
    el('sw-backups-body').addEventListener('click', (e) => {
      const b = e.target.closest('[data-restore]');
      if (b) restore(b.dataset.restore);
    });

    el('sched-add').addEventListener('click', addSchedule);
    el('sched-body').addEventListener('click', (e) => {
      const toggle = e.target.closest('[data-sched-toggle]');
      const del = e.target.closest('[data-sched-del]');
      if (toggle) api(`/api/schedules/${toggle.dataset.schedToggle}/toggle`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: toggle.dataset.enabled !== 'true' }),
      }).then(loadSchedules);
      if (del && confirm('Delete this schedule?')) api(`/api/schedules/${del.dataset.schedDel}`, { method: 'DELETE' }).then(loadSchedules);
    });
    el('fleet-run').addEventListener('click', runFleet);
    if (el('hist-device')) el('hist-device').addEventListener('change', loadHistory);
    if (el('hist-range')) el('hist-range').addEventListener('change', loadHistory);
    // Processes
    if (el('proc-device')) {
      el('proc-device').addEventListener('change', () => loadProcesses());
      el('proc-sort').addEventListener('change', () => loadProcesses());
      el('proc-refresh').addEventListener('click', () => loadProcesses());
      el('proc-auto').addEventListener('change', scheduleProcRefresh);
      el('proc-body').addEventListener('click', (e) => {
        const b = e.target.closest('[data-kill]');
        if (b) killProcess(b.dataset.kill, b.dataset.name);
      });
      el('svc-refresh').addEventListener('click', loadServices);
      el('svc-search').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadServices(); });
      el('svc-body').addEventListener('click', (e) => {
        const b = e.target.closest('[data-svc]');
        if (b) serviceAction(b.dataset.svc, b.dataset.act);
      });
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
  else wire();
})();
