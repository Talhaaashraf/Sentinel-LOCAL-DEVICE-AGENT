// Sentinel — Diagnose (AI), Stress Test and Decisions tabs.
// Loaded after app.js, so it shares its top-level helpers ($, setText) and the
// generatedToken / selectedPlatform state, and extends TAB_LOADERS and wireEvents.

(function () {
  const api = async (path, options) => {
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
    return data;
  };
  const el = (id) => document.getElementById(id);
  const esc = (text) => String(text == null ? '' : text).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

  let devices = [];
  let diagSession = null;
  let diagTimer = null;
  let stressCommandId = null;
  let stressTimer = null;

  async function loadDevices(selectId) {
    try {
      const data = await api('/api/agents');
      devices = data.agents || [];
    } catch (_e) {
      devices = [];
    }
    for (const id of ['diagnose-device', 'stress-device']) {
      const select = el(id);
      if (!select) continue;
      const current = select.value;
      const online = devices.filter((d) => d.status === 'online');
      select.innerHTML = devices.length
        ? devices.map((d) => `<option value="${d.device_id}">${esc(d.nickname)} — ${esc(d.os_type)}${d.status === 'online' ? '' : ' (offline)'}</option>`).join('')
        : '<option value="">No devices connected — add one from the Agents tab</option>';
      if (current) select.value = current;
      else if (online.length) select.value = online[0].device_id;
    }
  }

  // ---------------------------------------------------------------- Diagnose
  async function initDiagnose() {
    await loadDevices();
    try {
      const status = await api('/api/llm/status');
      el('diagnose-llm').textContent = status.available
        ? `Brain: ${status.provider} / ${status.model}`
        : `No LLM (${status.detail}) — using rule-based diagnosis`;
    } catch (_e) { el('diagnose-llm').textContent = 'AI status unknown'; }
    try {
      const data = await api('/api/presets');
      el('diagnose-presets').innerHTML = data.presets
        .map((p) => `<button class="preset-chip" data-preset="${p.id}">${esc(p.label)}</button>`)
        .join('');
    } catch (_e) { /* presets optional */ }
  }

  async function startDiagnose(preset) {
    const deviceId = el('diagnose-device').value;
    if (!deviceId) { alert('Pick a target laptop first (add one from the Agents tab).'); return; }
    const problem = el('diagnose-problem').value.trim();
    if (!problem && !preset) { el('diagnose-problem').focus(); return; }
    el('diagnose-session').classList.remove('hidden');
    el('diagnose-status').textContent = 'Starting…';
    el('diagnose-steps').innerHTML = '';
    el('diagnose-conclusion').classList.add('hidden');
    el('diagnose-fixes-wrap').classList.add('hidden');
    el('diagnose-outcome').classList.add('hidden');
    el('diagnose-correction').textContent = '';
    try {
      const data = await api('/api/diagnose/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: deviceId, problem, preset: preset || '' }),
      });
      diagSession = data.session_id;
      pollDiagnose();
    } catch (error) {
      el('diagnose-status').textContent = error.message;
    }
  }

  async function pollDiagnose() {
    clearTimeout(diagTimer);
    if (!diagSession) return;
    let session;
    try { session = await api(`/api/diagnose/${diagSession}`); }
    catch (_e) { diagTimer = setTimeout(pollDiagnose, 2000); return; }
    renderDiagnose(session);
    if (['running'].includes(session.status)) diagTimer = setTimeout(pollDiagnose, 1500);
  }

  function renderDiagnose(session) {
    const labels = { running: 'Investigating…', awaiting_decision: 'Diagnosis ready', closed: 'Closed', error: 'Error' };
    el('diagnose-status').textContent = labels[session.status] || session.status;
    el('diagnose-mode').textContent = session.mode ? `engine: ${session.mode}` : '';
    if (session.corrections && session.corrections.length) {
      el('diagnose-correction').textContent = 'Interpreted as: ' + session.corrected_problem;
    }
    if (session.similar_cases && session.similar_cases.length) {
      const box = el('diagnose-memory');
      box.classList.remove('hidden');
      box.innerHTML = '<strong>Remembered from past cases</strong>' + session.similar_cases
        .map((c) => `<div>• ${esc(c.symptom)} → ${esc(c.root_cause || 'n/a')} <em>(${esc(c.outcome || '?')})</em></div>`).join('');
    }
    el('diagnose-steps').innerHTML = (session.steps || []).map(renderStep).join('');
    if (session.conclusion) {
      const box = el('diagnose-conclusion');
      box.classList.remove('hidden');
      box.innerHTML = `<strong>Conclusion</strong><p>${esc(session.conclusion).replace(/\n/g, '<br>')}</p>`
        + (session.findings && session.findings.length
          ? '<ul>' + session.findings.map((f) => `<li><b>[${esc(f.severity)}]</b> ${esc(f.title)} — ${esc(f.detail)}</li>`).join('') + '</ul>'
          : '');
    }
    const fixes = session.proposed_fixes || [];
    if (fixes.length) {
      el('diagnose-fixes-wrap').classList.remove('hidden');
      el('diagnose-fixes').innerHTML = fixes.map(renderFix).join('');
    }
    if (session.status === 'awaiting_decision' || session.status === 'closed') {
      el('diagnose-outcome').classList.toggle('hidden', session.status === 'closed');
    }
  }

  function renderStep(step) {
    if (step.kind === 'tool_call') return `<div class="step step-call">▶ ${esc(step.tool)} <code>${esc(JSON.stringify(step.args || {}))}</code></div>`;
    if (step.kind === 'tool_result') {
      const ok = step.ok ? 'ok' : 'fail';
      const summary = step.ok ? summarizeResult(step.tool, step.result) : esc(step.error || 'failed');
      return `<div class="step step-${ok}">${step.ok ? '✓' : '✗'} ${esc(step.tool)} — ${summary}</div>`;
    }
    if (step.kind === 'assistant') return `<div class="step step-note">🧠 ${esc(step.text).slice(0, 400)}</div>`;
    if (step.kind === 'fix_executed') return `<div class="step step-call">⚙ ran ${esc(step.tool)} (${esc(step.status)})</div>`;
    return `<div class="step step-note">${esc(step.text || '')}</div>`;
  }

  function summarizeResult(tool, result) {
    if (!result || typeof result !== 'object') return 'done';
    if (result.browsers) return result.browsers.map((b) => `${b.browser} ${b.memory}`).join(', ') || 'no browsers running';
    if (result.processes) return `${result.processes.length} processes, RAM ${result.ram_used_percent}%`;
    if (result.volumes) return result.volumes.map((v) => `${v.mountpoint} ${v.percent}%`).join(', ');
    if (result.apps) return `${result.count} apps (${result.hidden_count} hidden, ${result.orphaned_count} orphaned)`;
    if (result.culprits) return `max CPU ${result.max_cpu}%, ${result.spike_seconds}s of spikes`;
    if (typeof result.pending_count === 'number') return `${result.pending_count} updates pending`;
    const keys = Object.keys(result).slice(0, 3).join(', ');
    return esc(keys || 'done');
  }

  function renderFix(fix) {
    const badge = fix.kind === 'read' ? '' : `<span class="fix-kind">${esc(fix.kind)}</span>`;
    const done = fix.status !== 'proposed';
    return `<div class="fix">
      <div class="fix-main"><strong>${esc(fix.title)}</strong> ${badge}
        <code>${esc(fix.tool)} ${esc(JSON.stringify(fix.args || {}))}</code>
        <p>${esc(fix.reason)}</p></div>
      <button class="primary-button" data-fix="${fix.id}" ${done ? 'disabled' : ''}>${done ? esc(fix.status) : 'Approve & run'}</button>
    </div>`;
  }

  async function approveFix(fixId) {
    try {
      await api(`/api/diagnose/${diagSession}/approve`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fix_id: fixId }),
      });
      pollDiagnose();
    } catch (error) { alert(error.message); }
  }

  async function recordOutcome(outcome) {
    const notes = prompt('Optional notes for the case record:') || '';
    try {
      await api(`/api/diagnose/${diagSession}/outcome`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ outcome, notes }),
      });
      el('diagnose-outcome').classList.add('hidden');
      el('diagnose-status').textContent = 'Saved to decisions ✓';
    } catch (error) { alert(error.message); }
  }

  // ---------------------------------------------------------------- Stress
  function initStress() { loadDevices(); }

  async function runStress(tool) {
    const deviceId = el('stress-device').value;
    if (!deviceId) { alert('Pick a target laptop first.'); return; }
    const args = {};
    if (tool === 'stress_cpu') args.seconds = Number(el('stress-cpu-seconds').value) || 60;
    if (tool === 'stress_ram') args.percent_of_free = Number(el('stress-ram-percent').value) || 60;
    if (tool === 'stress_disk') args.size_mb = Number(el('stress-disk-size').value) || 1024;
    if (tool === 'network_speed') args.size_mb = Number(el('stress-net-size').value) || 50;
    el('stress-result').classList.remove('hidden');
    el('stress-title').textContent = `${tool} — approved, waiting for the laptop…`;
    el('stress-progress').innerHTML = '';
    el('stress-output').textContent = '';
    el('stress-cancel').classList.remove('hidden');
    try {
      const command = await api(`/api/devices/${deviceId}/commands`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool, args, approve: true, reason: 'Stress test from dashboard' }),
      });
      stressCommandId = command.id;
      pollStress();
    } catch (error) { el('stress-title').textContent = error.message; }
  }

  async function pollStress() {
    clearTimeout(stressTimer);
    if (!stressCommandId) return;
    let command;
    try { command = await api(`/api/commands/${stressCommandId}`); }
    catch (_e) { stressTimer = setTimeout(pollStress, 2000); return; }
    el('stress-title').textContent = `${command.tool} — ${command.status}`;
    if (command.progress) {
      const line = document.createElement('div');
      line.className = 'step step-call';
      line.textContent = JSON.stringify(command.progress.progress || command.progress);
      const box = el('stress-progress');
      box.appendChild(line);
      if (box.childElementCount > 40) box.removeChild(box.firstChild);
      box.scrollTop = box.scrollHeight;
    }
    if (['done', 'error', 'cancelled', 'denied'].includes(command.status)) {
      el('stress-cancel').classList.add('hidden');
      el('stress-output').textContent = command.result ? JSON.stringify(command.result, null, 2) : (command.error || '');
      return;
    }
    stressTimer = setTimeout(pollStress, 1500);
  }

  async function cancelStress() {
    if (!stressCommandId) return;
    try { await api(`/api/commands/${stressCommandId}/cancel`, { method: 'POST' }); } catch (_e) { /* ignore */ }
  }

  // ---------------------------------------------------------------- Decisions
  async function loadDecisions() {
    try {
      const data = await api('/api/decisions/cases');
      el('decisions-cases').innerHTML = data.cases.length
        ? data.cases.map((c) => `<article class="decision-card">
            <div class="decision-top"><strong>${esc(c.symptom)}</strong><span class="outcome-pill ${esc(c.outcome)}">${esc(c.outcome)}</span></div>
            <small>${esc(c.hostname || '?')} · ${new Date(c.created_at).toLocaleString()} · ${esc(c.category || '')}</small>
            <p>${esc(c.root_cause || 'No root cause recorded.')}</p>
            ${(c.findings || []).slice(0, 3).map((f) => `<div class="finding">• ${esc(f.title)}</div>`).join('')}
          </article>`).join('')
        : '<div class="empty-state">No diagnoses have been recorded yet. Run one from the Diagnose tab.</div>';
    } catch (error) { el('decisions-cases').innerHTML = `<div class="empty-state">${esc(error.message)}</div>`; }
    try {
      const data = await api('/api/decisions/adrs');
      el('decisions-adrs').innerHTML = data.adrs.length
        ? data.adrs.map((a) => `<article class="decision-card"><strong>${esc(a.title)}</strong>
            <pre class="adr-body">${esc(a.markdown)}</pre></article>`).join('')
        : '<div class="empty-state">No design-decision records found.</div>';
    } catch (error) { el('decisions-adrs').innerHTML = `<div class="empty-state">${esc(error.message)}</div>`; }
  }

  // ---------------------------------------------------------------- Wiring
  function wire() {
    if (window.TAB_LOADERS) {
      window.TAB_LOADERS.diagnose = initDiagnose;
      window.TAB_LOADERS.stress = initStress;
      window.TAB_LOADERS.decisions = loadDecisions;
    }
    el('diagnose-start').addEventListener('click', () => startDiagnose());
    el('diagnose-problem').addEventListener('keydown', (e) => { if (e.key === 'Enter') startDiagnose(); });
    el('diagnose-presets').addEventListener('click', (e) => {
      const chip = e.target.closest('[data-preset]');
      if (chip) startDiagnose(chip.dataset.preset);
    });
    el('diagnose-fixes').addEventListener('click', (e) => {
      const button = e.target.closest('[data-fix]');
      if (button && !button.disabled) approveFix(button.dataset.fix);
    });
    el('diagnose-outcome').addEventListener('click', (e) => {
      const button = e.target.closest('[data-outcome]');
      if (button) recordOutcome(button.dataset.outcome);
    });
    document.querySelectorAll('[data-stress]').forEach((b) => b.addEventListener('click', () => runStress(b.dataset.stress)));
    el('stress-cancel').addEventListener('click', cancelStress);
    el('refresh-decisions').addEventListener('click', loadDecisions);
    document.querySelectorAll('.sub-tab').forEach((b) => b.addEventListener('click', () => {
      document.querySelectorAll('.sub-tab').forEach((x) => x.classList.toggle('active', x === b));
      el('decisions-cases').classList.toggle('hidden', b.dataset.sub !== 'cases');
      el('decisions-adrs').classList.toggle('hidden', b.dataset.sub !== 'adrs');
    }));
    const copyButton = el('copy-install');
    if (copyButton) copyButton.addEventListener('click', () => {
      navigator.clipboard.writeText(el('install-oneliner').textContent).then(() => {
        copyButton.textContent = 'Copied!';
        setTimeout(() => { copyButton.textContent = 'Copy command'; }, 1500);
      });
    });
    const persist = el('install-persist');
    if (persist && typeof window.updateInstallInstructions === 'function') {
      persist.addEventListener('change', window.updateInstallInstructions);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
  else wire();
})();
