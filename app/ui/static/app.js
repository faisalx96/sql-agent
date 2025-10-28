(() => {
  const { createApp, ref, onMounted, onUnmounted, nextTick } = Vue;

  // Markdown rendering enabled for assistant messages
  marked.setOptions({
    mangle: false,
    headerIds: false,
    highlight: (code, lang) => {
      try { return hljs.highlightAuto(code, lang ? [lang] : undefined).value; }
      catch { return code; }
    }
  });
  // Sanitize output to prevent XSS
  function sanitize(html) { return DOMPurify.sanitize(html); }

  createApp({
    setup() {
      const chatId = ref(null);
      const messages = ref([]);
      const input = ref('');
      const inputEl = ref(null);
      const model = ref('');
      const allowedModels = ref([]);
      const streaming = ref(false);
      const sidebarOpen = ref(false);
      const sessions = ref([]); // {id, title, created_at, updated_at}
      const loadingSessions = ref(false);
      const loadingChat = ref(false);
      const showSettings = ref(false);
      const settings = ref({ theme: 'dark', previewRowsCollapsed: 10, reducedMotion: false });
      const toasts = ref([]);
      let controller = null;
      let onKey = null;
      const tick = ref(0);
      let tickTimer = null;
      const turnStart = ref(null);

      // Server-backed sessions
      async function refreshSessions() {
        loadingSessions.value = true;
        try {
          const r = await fetch('/api/sessions');
          if (r.ok) {
            const d = await r.json();
            sessions.value = Array.isArray(d.sessions) ? d.sessions : [];
          }
        } catch {}
        finally { loadingSessions.value = false; }
      }
      function sortedSessions() { return [...sessions.value].sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0)); }
      function toast(msg, ms = 1400) { const id = Math.random().toString(36).slice(2, 9); toasts.value.push({ id, msg }); setTimeout(() => { const i = toasts.value.findIndex(t => t.id === id); if (i >= 0) toasts.value.splice(i, 1); }, ms); }

      // Settings
      const SETTINGS_KEY = 'sqlagent:settings';
      function loadSettings() {
        try {
          const raw = localStorage.getItem(SETTINGS_KEY);
          if (raw) {
            const parsed = JSON.parse(raw);
            if (parsed && typeof parsed === 'object') Object.assign(settings.value, parsed);
          }
        } catch {}
        applyTheme();
      }
      function saveSettings() {
        try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings.value)); } catch {}
        applyTheme();
      }
      function applyTheme() {
        const theme = settings.value.theme || 'dark';
        document.documentElement.setAttribute('data-theme', theme);
        if (settings.value.reducedMotion) document.documentElement.setAttribute('data-reduce-motion', '1');
        else document.documentElement.removeAttribute('data-reduce-motion');
      }

      // UI helpers
      function scrollToBottom() { const el = document.getElementById('chat'); if (el) el.scrollTop = el.scrollHeight; }
      function autosize() { const el = inputEl.value; if (!el) return; el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 180) + 'px'; }

      // Rendering helpers
      function renderMarkdown(text) { return sanitize(marked.parse(text || '')); }
      function renderCode(obj) {
        try { const json = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2); const highlighted = hljs.highlight(json, { language: 'json' }).value; return sanitize(highlighted); }
        catch (e) { const str = (obj == null) ? '' : String(obj); return sanitize(str); }
      }
      function formatSQL(sql) {
        if (!sql || typeof sql !== 'string') return '';
        let s = sql.trim().replace(/\s+/g, ' ');
        const clauses = ['SELECT','FROM','LEFT JOIN','RIGHT JOIN','INNER JOIN','OUTER JOIN','JOIN','WHERE','GROUP BY','HAVING','ORDER BY','LIMIT','OFFSET','UNION ALL','UNION','WITH','ON','AND','OR'];
        for (const c of clauses) { const re = new RegExp(`\\s+(${c.replace(/ /g,'\\s+')})\\s+`, 'gi'); s = s.replace(re, (m,p1) => `\n${p1.toUpperCase()} `); }
        s = s.replace(/,\s*/g, ',\n  ');
        const lines = s.split(/\n+/); const out = []; let indent = 0;
        const base0 = /^(SELECT|FROM|WHERE|GROUP BY|HAVING|ORDER BY|LIMIT|OFFSET|WITH|UNION|UNION ALL)\b/i;
        const base1 = /^(AND|OR|JOIN|LEFT JOIN|RIGHT JOIN|INNER JOIN|OUTER JOIN|ON)\b/i;
        for (let line of lines) { const open = (line.match(/\(/g) || []).length; const close = (line.match(/\)/g) || []).length; if (close > open && indent > 0) indent -= (close - open); const pad = base0.test(line) ? 0 : (base1.test(line) ? 2 : Math.max(indent, 0)); out.push(' '.repeat(pad) + line.trim()); if (open > close) indent += (open - close); }
        return out.join('\n');
      }
      function renderSQL(sql) { try { const formatted = formatSQL(sql); const highlighted = hljs.highlight(formatted || '', { language: 'sql' }).value; return sanitize(highlighted); } catch (e) { return sanitize(sql || ''); } }
      // Chart rendering (Chart.js via CDN)
      function renderFallbackChart(t, labels, data) {
        try {
          const id = t._fbId; if (!id) return;
          const el = document.getElementById(id); if (!el) return;
          el.innerHTML = '';
          const max = Math.max(1, ...data.map(v => (typeof v === 'number' ? v : Number(v) || 0)));
          labels.forEach((lab, i) => {
            const v = (typeof data[i] === 'number' ? data[i] : Number(data[i]) || 0);
            const row = document.createElement('div'); row.style.display = 'flex'; row.style.alignItems = 'center'; row.style.gap = '8px'; row.style.margin = '4px 0';
            const l = document.createElement('div'); l.textContent = String(lab); l.style.width = '160px'; l.style.fontSize = '12px'; l.style.color = '#94a3b8'; l.style.whiteSpace = 'nowrap'; l.style.overflow = 'hidden'; l.style.textOverflow = 'ellipsis';
            const barWrap = document.createElement('div'); barWrap.style.flex = '1'; barWrap.style.height = '10px'; barWrap.style.background = 'rgba(148, 163, 184, 0.15)'; barWrap.style.border = '1px solid rgba(148,163,184,0.2)'; barWrap.style.borderRadius = '6px';
            const bar = document.createElement('div'); bar.style.height = '100%'; bar.style.width = `${Math.max(2, (v / max) * 100)}%`; bar.style.background = '#60a5fa'; bar.style.borderRadius = '6px';
            barWrap.appendChild(bar);
            const val = document.createElement('div'); val.textContent = String(v); val.style.width = '44px'; val.style.textAlign = 'right'; val.style.fontSize = '12px'; val.style.color = '#cbd5e1';
            row.appendChild(l); row.appendChild(barWrap); row.appendChild(val);
            el.appendChild(row);
          });
        } catch {}
      }

      function renderChartForTool(t, retry = 0) {
        try {
          if (!t || t.name !== 'display_chart') return;
          if (typeof Chart === 'undefined') { if (retry < 10) setTimeout(() => renderChartForTool(t, retry+1), 100); return; }
          try { if (!Chart._sqlAgentRegistered && Chart.register && Chart.registerables) { Chart.register(...Chart.registerables); Chart._sqlAgentRegistered = true; } } catch {}
          const id = t._chartId; if (!id) return;
          const canvas = document.getElementById(id);
          if (!canvas) { if (retry < 10) setTimeout(() => renderChartForTool(t, retry+1), 100); return; }
          // If hidden (e.g., parent collapsed), retry shortly
          const isHidden = !canvas.offsetParent || canvas.clientWidth === 0;
          if (isHidden && retry < 10) { setTimeout(() => renderChartForTool(t, retry+1), 120); return; }
          const ctx = canvas.getContext('2d');
          if (!ctx) return;
          // Destroy prior instance to avoid leaks
          if (t._chart) { try { t._chart.destroy(); } catch {} t._chart = null; }
          const spec = t.output || {};
          const columns = spec.columns || (spec.data && spec.data.columns) || [];
          const rows = spec.rows || (spec.data && spec.data.rows) || [];
          if (!Array.isArray(columns) || !Array.isArray(rows) || !rows.length) return;
          const xName = spec.x || (columns[0] || null);
          let series = Array.isArray(spec.series) && spec.series.length ? spec.series : (spec.y ? [spec.y] : columns.filter(c => c !== xName).slice(0,3));
          const type = (spec.type === 'bar') ? 'bar' : 'line';
          const stacked = !!spec.stacked;
          const xi = columns.indexOf(xName);
          if (xi < 0) return;
          const labels = rows.map(r => r[xi]);
          const palette = ['#60a5fa','#34d399','#f472b6','#f59e0b','#a78bfa','#22d3ee'];
          const datasets = [];
          series.forEach((sname, i) => {
            const si = columns.indexOf(sname);
            if (si < 0) return;
            const color = palette[i % palette.length];
            const data = rows.map(r => (typeof r[si] === 'number' ? r[si] : Number(r[si]) || 0));
            const base = { label: sname, data, parsing: false, borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: 0, tension: 0.2 };
            if (type === 'line') { base.fill = (spec.type === 'area'); }
            datasets.push(base);
          });
          t._chartStatus = { labels: labels.length, datasets: datasets.length };
          if (!datasets.length || !labels.length) {
            // Fallback to simple in-DOM bars using the first series if available
            const fb = (datasets[0] && datasets[0].data) || [];
            renderFallbackChart(t, labels, fb);
            return;
          }
          // Ensure the canvas has width/height (Chart.js uses CSS size)
          canvas.style.height = canvas.style.height || '320px';
          if (!canvas.style.width || canvas.clientWidth === 0) canvas.style.width = '100%';
          const options = {
            responsive: true,
            maintainAspectRatio: false,
            scales: { x: { type: 'category', stacked }, y: { type: 'linear', stacked, beginAtZero: true } },
            plugins: { legend: { display: true }, title: { display: !!spec.title, text: spec.title || '' } }
          };
          t._chart = new Chart(ctx, { type, data: { labels, datasets }, options });
          try { if (typeof t._chart.resize === 'function') t._chart.resize(); } catch {}
        } catch {}
      }

      // Time + duration
      function toolElapsed(t) {
        void tick.value;
        if (!t) return null;
        if (!t.start && !t.end) { return t.output ? null : 0; }
        const start = t.start || Date.now();
        const end = (t.output && t.end) ? t.end : Date.now();
        return Math.max(0, end - start);
      }
      function formatDuration(ms) { if (ms == null) return ''; if (ms < 1000) return `${Math.round(ms)} ms`; const s = ms / 1000; if (s < 10) return `${s.toFixed(2)} s`; if (s < 60) return `${s.toFixed(1)} s`; const m = Math.floor(s / 60); const rem = Math.round(s - m * 60); return `${m}m ${rem}s`; }
      function turnElapsed() { void tick.value; if (!streaming.value || !turnStart.value) return 0; return Date.now() - turnStart.value; }
      function formatTimeAgo(ts) { if (!ts) return ''; const s = Math.floor((Date.now() - ts) / 1000); if (s < 60) return `${s}s ago`; const m = Math.floor(s / 60); if (m < 60) return `${m}m ago`; const h = Math.floor(m / 60); if (h < 24) return `${h}h ago`; const d = Math.floor(h / 24); return `${d}d ago`; }

      // Clipboard helpers
      function copyText(text) { navigator.clipboard.writeText(text || ''); toast('Copied'); }
      function copyJSON(obj) { try { const json = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2); navigator.clipboard.writeText(json); toast('JSON copied'); } catch { navigator.clipboard.writeText(String(obj ?? '')); toast('Copied'); } }
      function copyCSV(preview) { try { const cols = preview.columns || []; const rows = preview.rows || []; const escape = (v) => { if (v == null) return ''; const s = String(v); return /[",\n]/.test(s) ? '"' + s.replace(/\"/g, '\"\"') + '"' : s; }; const header = cols.map(escape).join(','); const body = rows.map(r => cols.map((_, i) => escape(r[i])).join(',')).join('\n'); const csv = header + '\n' + body; navigator.clipboard.writeText(csv); toast('CSV copied'); } catch {} }
      function downloadCSV(preview) { try { const cols = preview.columns || []; const rows = preview.rows || []; const escape = (v) => { if (v == null) return ''; const s = String(v); return /[",\n]/.test(s) ? '"' + s.replace(/\"/g, '\"\"') + '"' : s; }; const header = cols.map(escape).join(','); const body = rows.map(r => cols.map((_, i) => escape(r[i])).join(',')).join('\n'); const csv = header + '\n' + body; const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' }); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = 'result.csv'; a.click(); URL.revokeObjectURL(url); toast('CSV downloaded'); } catch {} }

      // Sessions lifecycle
      async function ensureChat() {
        if (!chatId.value) {
          const res = await fetch('/api/new_chat', { method: 'POST' });
          const data = await res.json();
          chatId.value = data.chat_id;
          await refreshSessions();
        }
      }
      async function createChat() {
        const res = await fetch('/api/new_chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model: model.value || undefined }) });
        const data = await res.json();
        chatId.value = data.chat_id;
        messages.value = [];
        input.value = '';
        await refreshSessions();
      }
      async function newChat() { await createChat(); }
      async function selectChat(id) {
        if (!id) return;
        chatId.value = id;
        input.value = '';
        sidebarOpen.value = false;
        loadingChat.value = true;
        try {
          const r = await fetch(`/api/sessions/${encodeURIComponent(id)}`);
          if (r.ok) {
            const d = await r.json();
            if (d && d.model) { model.value = d.model; }
            const list = Array.isArray(d.messages) ? d.messages : [];
            // Reconstruct assistant tool panels by pairing tool_calls with tool results
            const out = [];
            let pendingTools = null;
            function parseArgs(raw) {
              try { return typeof raw === 'string' ? JSON.parse(raw) : raw; } catch { return raw; }
            }
            function parseJSON(raw) {
              try { return typeof raw === 'string' ? JSON.parse(raw) : raw; } catch { return raw; }
            }
            for (const m of list) {
              if (!m) continue;
              if (m.role === 'assistant' && m.tool_calls) {
                pendingTools = (m.tool_calls || []).map(tc => ({
                  id: tc.id,
                  name: tc.function && tc.function.name,
                  arguments: parseArgs(tc.function && tc.function.arguments),
                  title: (() => { const a = parseArgs(tc.function && tc.function.arguments); return a && a.title; })(),
                  output: undefined,
                  expanded: false,
                  start: null,
                  end: null,
                }));
                // Skip adding this intermediate assistant message
                continue;
              }
              if (m.role === 'tool') {
                if (pendingTools) {
                  const t = pendingTools.find(x => x.id === m.tool_call_id);
                  if (t) t.output = parseJSON(m.content);
                }
                continue;
              }
              if (m.role === 'user') {
                out.push({ role: 'user', content: m.content || '' });
                continue;
              }
              if (m.role === 'assistant') {
                const amsg = { role: 'assistant', content: m.content || '', renderRaw: false };
                if (m.thinking) { amsg.thinking = m.thinking; amsg.thinkingExpanded = true; }
                if (pendingTools && pendingTools.length) {
                  amsg.tools = pendingTools.map(t => ({ ...t }));
                  // If we have a result, attach preview (supports sql_query or display_result)
                  const sqlTool = amsg.tools.find(t => (t.name === 'display_result' || t.name === 'sql_query') && t.output && t.output.columns && t.output.rows);
                  if (sqlTool) {
                    const o = sqlTool.output;
                    amsg.preview = { columns: o.columns, rows: o.rows, rowcount: o.rowcount };
                    amsg.previewExpanded = false;
                  }
                }
                if (m.duration_ms != null) amsg.totalMs = m.duration_ms;
                out.push(amsg);
                pendingTools = null;
              }
            }
            // If tools existed without a final assistant, show them as a separate assistant block
            if (pendingTools && pendingTools.length) {
              const amsg = { role: 'assistant', content: '', tools: pendingTools.map(t => ({ ...t })), renderRaw: false };
              const sqlTool = amsg.tools.find(t => (t.name === 'display_result' || t.name === 'sql_query') && t.output && t.output.columns && t.output.rows);
              if (sqlTool) { const o = sqlTool.output; amsg.preview = { columns: o.columns, rows: o.rows, rowcount: o.rowcount }; amsg.previewExpanded = false; }
              out.push(amsg);
            }
            messages.value = out;
            await nextTick();
            try {
              for (const m of messages.value) {
                for (const t of (m.tools || [])) {
                  if (t.name === 'display_chart' && t.output) {
                    t.expanded = true;
                    t._chartId = t._chartId || ('chart-' + Math.random().toString(36).slice(2,9));
                    renderChartForTool(t);
                  }
                }
              }
            } catch {}
          } else {
            messages.value = [];
          }
        } catch { messages.value = []; }
        finally { loadingChat.value = false; }
      }
      async function renameChat(s) {
        const name = prompt('Rename chat', s.title || '');
        if (name == null) return;
        try {
          const r = await fetch(`/api/sessions/${encodeURIComponent(s.id)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: String(name).trim() }) });
          if (r.ok) { await refreshSessions(); }
        } catch {}
      }
      async function deleteChat(id) {
        if (!confirm('Delete this chat?')) return;
        try { await fetch(`/api/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }); } catch {}
        await refreshSessions();
        if (chatId.value === id) {
          chatId.value = null; messages.value = [];
          const next = sortedSessions()[0];
          if (next) await selectChat(next.id); else await createChat();
        }
      }

      // Streaming chat
      async function sendMessage(text) {
        await ensureChat();
        const userMsg = { role: 'user', content: text };
        messages.value.push(userMsg);
        const assistantMsg = { role: 'assistant', content: '', renderRaw: false };
        messages.value.push(assistantMsg);
        // If current session has no title, set one from the first user message
        const sess = sessions.value.find(s => s.id === chatId.value);
        if (sess && !sess.title) {
          const t = text.replace(/\s+/g,' ').trim();
          const title = t.slice(0, 60) + (t.length > 60 ? '…' : '');
          try { await fetch(`/api/sessions/${encodeURIComponent(chatId.value)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title }) }); } catch {}
          await refreshSessions();
        }
        await nextTick(); scrollToBottom();

        controller = new AbortController();
        streaming.value = true; turnStart.value = Date.now();
        if (!tickTimer) { tickTimer = setInterval(() => { tick.value = (tick.value + 1) | 0; }, 100); }
        try {
          const res = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ chat_id: chatId.value, message: text }), signal: controller.signal });
          if (!res.ok || !res.body) {
            const msg = await res.text().catch(() => '');
            assistantMsg.content += (assistantMsg.content ? '\n\n' : '') + `_(error ${res.status}: ${msg.slice(0,200)})_`;
            return;
          }
          const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
          while (true) {
            const { value, done } = await reader.read(); if (done) break;
            for (const line of value.split('\n')) {
              if (!line.trim()) continue;
              try {
                const evt = JSON.parse(line);
                if (evt.type === 'tool_call') {
                  if (!assistantMsg.tools) assistantMsg.tools = [];
                  const existing = assistantMsg.tools.find(t => t.id === evt.id);
                  if (existing) { existing.name = evt.name; existing.arguments = evt.arguments; existing.title = (evt.arguments && evt.arguments.title) || existing.title; }
                  else { assistantMsg.tools.push({ id: evt.id, name: evt.name, title: (evt.arguments && evt.arguments.title) || undefined, arguments: evt.arguments, output: undefined, expanded: false, start: Date.now(), end: null }); }
                  await nextTick();
                } else if (evt.type === 'tool_result') {
                  if (!assistantMsg.tools) assistantMsg.tools = [];
                  const existing = assistantMsg.tools.find(t => t.id === evt.id);
                  if (existing) { existing.output = evt.output; existing.end = Date.now(); }
                  else { assistantMsg.tools.push({ id: evt.id, name: evt.name, arguments: undefined, output: evt.output, expanded: false, start: Date.now(), end: Date.now() }); }
                  if ((evt.name === 'display_result' || evt.name === 'sql_query') && evt.output && evt.output.columns && evt.output.rows) { assistantMsg.preview = { columns: evt.output.columns, rows: evt.output.rows, rowcount: evt.output.rowcount }; assistantMsg.previewExpanded = false; }
                  if (evt.name === 'display_chart') {
                    const tool = existing || (assistantMsg.tools.find(t => t.id === evt.id));
                    if (tool) {
                      tool.expanded = true;
                      tool._chartId = tool._chartId || ('chart-' + Math.random().toString(36).slice(2,9));
                      await nextTick();
                      renderChartForTool(tool);
                      setTimeout(() => renderChartForTool(tool), 150);
                      setTimeout(() => renderChartForTool(tool), 500);
                    }
                  }
                  await nextTick();
                } else if (evt.tools) {
                  assistantMsg.tools = evt.tools.map(t => ({ ...t, expanded: false }));
                  await nextTick();
                } else if (evt.type === 'thinking' && evt.content != null) {
                  const add = String(evt.content);
                  if (assistantMsg.thinking) assistantMsg.thinking += add;
                  else assistantMsg.thinking = add;
                  assistantMsg.thinkingExpanded = true;
                  await nextTick();
                } else if (evt.error) {
                  assistantMsg.content += (assistantMsg.content ? '\n\n' : '') + `_(error: ${String(evt.error)})_`;
                  await nextTick();
                } else if (evt.chunk) {
                  assistantMsg.content += evt.chunk;
                  await nextTick(); scrollToBottom();
                } else if (evt.done) {
                  // no-op
                }
              } catch {}
            }
          }
        } catch (e) {
          if (e.name !== 'AbortError') { assistantMsg.content += '\n\n_(stream error)_'; }
        } finally {
          // Store total duration on the assistant message
          try { assistantMsg.totalMs = (turnStart.value != null) ? (Date.now() - turnStart.value) : undefined; } catch {}
          streaming.value = false; controller = null; turnStart.value = null; await refreshSessions();
        }
      }

      async function onSubmit() { const text = (input.value || '').trim(); if (!text || streaming.value) return; input.value = ''; await sendMessage(text); }
      function stopStreaming() { if (controller) controller.abort(); }
      const canRetry = Vue.computed(() => { const lastUser = [...messages.value].reverse().find(m => m.role === 'user'); return !!lastUser && !streaming.value; });
      async function retryLast() { if (streaming.value) return; const lastUser = [...messages.value].reverse().find(m => m.role === 'user'); if (!lastUser) return; await sendMessage(lastUser.content); }

      onMounted(async () => {
        loadSettings();
        // Close settings on Escape
        onKey = (e) => { if (e.key === 'Escape') showSettings.value = false; };
        window.addEventListener('keydown', onKey);
        try { const r = await fetch('/api/meta'); if (r.ok) { const d = await r.json(); model.value = d.model || ''; allowedModels.value = Array.isArray(d.allowed_models) ? d.allowed_models : []; } } catch {}
        await refreshSessions();
        const first = sortedSessions()[0];
        if (first) {
          await selectChat(first.id);
        } else {
          await createChat();
        }
        showSettings.value = false;
      });
      onUnmounted(() => { if (tickTimer) clearInterval(tickTimer); if (onKey) window.removeEventListener('keydown', onKey); });

      // Tool UI helpers
      function toolSummary(t) {
        try {
          if ((t.name === 'display_result' || t.name === 'sql_query') && t.output) {
            const rc = t.output.rowcount ?? (t.output.rows ? t.output.rows.length : undefined);
            const cc = t.output.columns ? t.output.columns.length : undefined;
            const parts = []; if (rc != null) parts.push(`${rc} rows`); if (cc != null) parts.push(`${cc} cols`);
            return parts.join(', ') || 'result';
          }
          if (t.name === 'display_chart' && t.output) {
            const type = t.output.type || 'line';
            const x = t.output.x || '';
            const s = Array.isArray(t.output.series) ? t.output.series.join(', ') : (t.output.y || '');
            return `${type} ${x}${s ? ' vs ' + s : ''}`.trim();
          }
          if (t.name === 'sql_schema' && t.output) {
            const tables = Array.isArray(t.output.tables) ? t.output.tables.length : (t.output.tables ? Object.keys(t.output.tables).length : 0);
            return `${tables} tables`;
          }
          if (t.name === 'list_files' && t.output) { return `${(t.output.files||[]).length} files`; }
          if (t.name === 'search_files' && t.output) { return `${(t.output.hits||[]).length} matches for "${(t.output.query||'').toString().slice(0,32)}"`; }
          if (t.name === 'read_file' && t.output) { const p = t.output.path || ''; const len = (t.output.content||'').length; return `${p} (${len} chars)`; }
          if (t.name === 'write_file' && t.output) { const p = t.output.path || ''; const b = t.output.bytes; return `wrote ${b} bytes to ${p}`; }
        } catch {}
        return 'details';
      }
      function toggleTool(t) { t.expanded = !t.expanded; if (t.expanded) { nextTick(() => renderChartForTool(t)); } }

      // Tool UI: type, filter, bulk expand/collapse, errors, downloads
      function displayTitle(t) {
        if (t && t.title) return String(t.title);
        try {
          if (t && t.name === 'sql_query' && t.arguments && t.arguments.sql) {
            const s = String(t.arguments.sql).trim().replace(/\s+/g,' ');
            return s.slice(0, 60) + (s.length > 60 ? '…' : '');
          }
        } catch {}
        return (t && t.name) || 'tool';
      }
      function toolType(t) {
        const n = (t && t.name) || '';
        if (n === 'sql_query' || n === 'sql_schema') return 'sql';
        if (n === 'list_files' || n === 'read_file' || n === 'write_file' || n === 'search_files') return 'files';
        return 'other';
      }
      function visibleToolsFor(m) {
        const f = m.toolFilter || 'all';
        if (!m.tools) return [];
        if (f === 'all') return m.tools;
        return m.tools.filter(t => toolType(t) === f);
      }
      function setToolFilter(m, f) { m.toolFilter = f; }
      function expandAll(m) { (m.tools||[]).forEach(t => t.expanded = true); }
      function collapseAll(m) { (m.tools||[]).forEach(t => t.expanded = false); }
      function toolError(t) {
        try {
          const o = t && t.output;
          if (!o) return null;
          if (typeof o === 'string') {
            const s = o.toLowerCase();
            if (s.includes('error') || s.includes('exception')) return 'Error';
            return null;
          }
          if (typeof o === 'object') {
            if (o.error) return String(o.error);
            if (o.stderr) return String(o.stderr).slice(0, 140);
            if (o.ok === false) return 'Failed';
          }
        } catch {}
        return null;
      }
      function downloadJSONData(obj, filename = 'tool_output.json') {
        try {
          const json = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2);
          const blob = new Blob([json], { type: 'application/json' });
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a'); a.href = url; a.download = filename; a.click(); URL.revokeObjectURL(url);
          toast('JSON downloaded');
        } catch {}
      }

      function toggleRender(m) { m.renderRaw = !m.renderRaw; }

      async function applyModel(newModel) {
        if (!chatId.value) return;
        const name = String(newModel || '').trim();
        if (!name) return;
        try {
          await fetch(`/api/sessions/${encodeURIComponent(chatId.value)}`, {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model: name })
          });
          model.value = name;
          toast(`Model set to ${name}`);
        } catch {}
      }

      function previewRowCount(preview) { if (!preview) return 0; if (preview.rowcount !== undefined && preview.rowcount !== null) return preview.rowcount; if (Array.isArray(preview.rows)) return preview.rows.length; return 0; }

      return { chatId, messages, input, inputEl, streaming, model, allowedModels, sidebarOpen, sessions, loadingSessions, loadingChat, showSettings, settings, toasts, newChat, createChat, selectChat, renameChat, deleteChat, onSubmit, stopStreaming, retryLast, canRetry, copyText, renderMarkdown, renderCode, renderSQL, toolSummary, displayTitle, toggleTool, toolError, copyJSON, copyCSV, downloadCSV, previewRowCount, toolElapsed, formatDuration, turnElapsed, sortedSessions, formatTimeAgo, autosize, saveSettings, toggleRender, applyModel, renderChartForTool };
    }
  }).mount('#app');
})();
