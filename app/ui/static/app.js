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
      const loadingModels = ref(false);
      const modelOpen = ref(false);
      const modelQuery = ref('');
      const streaming = ref(false);
      const sidebarOpen = ref(false);
      const sessions = ref([]); // {id, title, created_at, updated_at}
      const loadingSessions = ref(false);
      const loadingChat = ref(false);
      const showSettings = ref(false);
      const settings = ref({ theme: 'dark', previewRowsCollapsed: 10, reducedMotion: false, showThinking: true });
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

      // Inline charts in assistant Markdown via ```chart code blocks
      const inlineCharts = {};
      function ensureChartRegistered() { try { if (typeof Chart !== 'undefined' && Chart.register && Chart.registerables && !Chart._sqlAgentRegistered) { Chart.register(...Chart.registerables); Chart._sqlAgentRegistered = true; } } catch {} }
      function buildDatasetsFromSpec(spec) {
        const columns = spec.columns || (spec.data && spec.data.columns) || [];
        const rows = spec.rows || (spec.data && spec.data.rows) || [];
        if (!Array.isArray(columns) || !Array.isArray(rows) || !rows.length) return null;
        const xName = spec.x || (columns[0] || null);
        const rawType = String(spec.type || 'line').toLowerCase();
        const t = (rawType === 'area') ? 'line' : (rawType === 'bar_horizontal' || rawType === 'bar-horizontal' || rawType === 'horizontal-bar') ? 'bar' : rawType;
        const isScatter = (t === 'scatter');
        const isPie = (t === 'pie');
        const isDoughnut = (t === 'doughnut');
        const xi = columns.indexOf(xName); if (xi < 0 && !isScatter) return null;
        const labels = isScatter ? [] : rows.map(r => r[xi]);
        const palette = ['#60a5fa','#34d399','#f472b6','#f59e0b','#a78bfa','#22d3ee'];
        let datasets;
        if (isScatter) {
          const yName = spec.y || (columns.find(c => c !== xName) || null);
          const yi = columns.indexOf(yName); if (yi < 0) return null;
          const color = palette[0];
          const data = rows.map(r => ({ x: Number(r[xi]) || 0, y: Number(r[yi]) || 0 }));
          datasets = [{ label: yName || 'series', data, borderColor: color, backgroundColor: color + '88' }];
        } else if (isPie || isDoughnut) {
          const yName = spec.y || (columns.find(c => c !== xName) || null);
          const yi = columns.indexOf(yName); if (yi < 0) return null;
          const data = rows.map(r => (typeof r[yi] === 'number' ? r[yi] : Number(r[yi]) || 0));
          datasets = [{ label: yName || 'value', data, backgroundColor: labels.map((_, i) => palette[i % palette.length]) }];
        } else {
          const series = Array.isArray(spec.series) && spec.series.length ? spec.series : (spec.y ? [spec.y] : columns.filter(c => c !== xName).slice(0,3));
          datasets = series.map((sname, i) => {
            const si = columns.indexOf(sname); if (si < 0) return null;
            const color = palette[i % palette.length];
            const data = rows.map(r => (typeof r[si] === 'number' ? r[si] : Number(r[si]) || 0));
            const base = { label: sname, data, borderColor: color, backgroundColor: color + '88', borderWidth: 2, pointRadius: 0, tension: 0.2 };
            if (rawType === 'area') base.fill = true;
            return base;
          }).filter(Boolean);
        }
        if (!datasets || !datasets.length) return null;
        const options = {};
        if (rawType === 'bar_horizontal' || rawType === 'bar-horizontal' || rawType === 'horizontal-bar') options.indexAxis = 'y';
        return { labels, datasets, type: (t || 'line'), stacked: !!spec.stacked, title: spec.title || '', options };
      }
      function isChartSpec(obj) {
        try {
          if (!obj || typeof obj !== 'object') return false;
          const cols = obj.columns || (obj.data && obj.data.columns);
          const rows = obj.rows || (obj.data && obj.data.rows);
          if (!Array.isArray(cols) || !Array.isArray(rows) || rows.length === 0) return false;
          // Heuristic: allow missing axis hints; we'll default x to first column and y/series to remaining
          // Heuristics to avoid false positives
          if (cols.length > 64) return false;
          if (rows.length > 2000) return false;
          return true;
        } catch { return false; }
      }
      function renderInlineChartsForMessage(idx, attempt = 0) {
        try {
          // If Chart isn't ready yet, retry shortly (initial page load)
          if (typeof Chart === 'undefined') { if (attempt < 25) setTimeout(() => renderInlineChartsForMessage(idx, attempt+1), 120); return; }
          ensureChartRegistered();
          const root = document.getElementById('msg-content-' + idx);
          if (!root) return;
          const blocks = root.querySelectorAll('pre code');
          blocks.forEach((codeEl, i) => {
            const pre = codeEl.parentElement;
            if (!pre || pre.dataset.chartRendered) return;
            const lang = (codeEl.className || '').toString();
            const raw = codeEl.textContent || '';
            let spec = null;
            // Prefer explicit ```chart blocks, but also accept JSON that looks like a chart spec
            if (/\blanguage-chart\b/.test(lang)) {
              try { spec = JSON.parse(raw); } catch { spec = null; }
            } else if (/\blanguage-json\b/.test(lang) || !lang) {
              try { const parsed = JSON.parse(raw); if (isChartSpec(parsed)) spec = parsed; } catch { spec = null; }
            }
            if (!spec) return;
            // Fill sensible defaults for missing axis hints
            try {
              if (!spec.x && Array.isArray(spec.columns) && spec.columns.length) spec.x = spec.columns[0];
              if (!spec.y && !spec.series && Array.isArray(spec.columns) && spec.columns.length > 1) {
                spec.series = spec.columns.filter(c => c !== spec.x).slice(0, 3);
              }
            } catch {}
            const ds = buildDatasetsFromSpec(spec); if (!ds) return;
            const canvas = document.createElement('canvas');
            const cid = 'mc-' + idx + '-' + i + '-' + Math.random().toString(36).slice(2,6);
            canvas.id = cid; canvas.style.width = '100%'; canvas.style.height = '280px';
            const wrap = document.createElement('div'); wrap.className = 'my-2'; wrap.appendChild(canvas);
            pre.replaceWith(wrap);
            wrap.dataset.chartRendered = '1';
            const ctx = canvas.getContext('2d'); if (!ctx || typeof Chart === 'undefined') return;
            try { if (inlineCharts[cid]) { try { inlineCharts[cid].destroy(); } catch {} } } catch {}
            const options = { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: true }, title: { display: !!ds.title, text: ds.title } } };
            if (ds.type === 'pie' || ds.type === 'doughnut') {
              // no scales
            } else if (ds.type === 'scatter') {
              options.scales = { x: { type: 'linear', beginAtZero: true }, y: { type: 'linear', beginAtZero: true } };
            } else {
              options.scales = { x: { stacked: ds.stacked }, y: { stacked: ds.stacked, beginAtZero: true } };
            }
            if (ds.options && ds.options.indexAxis) { options.indexAxis = ds.options.indexAxis; }
            const chart = new Chart(ctx, { type: ds.type, data: { labels: ds.labels, datasets: ds.datasets }, options });
            inlineCharts[cid] = chart;
          });
        } catch {}
      }
      // Chart rendering (Chart.js via CDN)
      function renderChartForTool(t) {
        try {
          if (!t || t.name !== 'display_chart') return;
          if (!t.expanded) return; // render only when visible
          if (typeof Chart === 'undefined') return;
          try { if (!Chart._sqlAgentRegistered && Chart.register && Chart.registerables) { Chart.register(...Chart.registerables); Chart._sqlAgentRegistered = true; } } catch {}
          const id = t._chartId; if (!id) return;
          const canvas = document.getElementById(id);
          if (!canvas) return;
          const ctx = canvas.getContext('2d'); if (!ctx) return;
          if (t._chart) { try { t._chart.destroy(); } catch {} t._chart = null; }
          const spec = t.output || {};
          const columns = spec.columns || (spec.data && spec.data.columns) || [];
          const rows = spec.rows || (spec.data && spec.data.rows) || [];
          if (!Array.isArray(columns) || !Array.isArray(rows) || !rows.length) return;
          const xName = spec.x || (columns[0] || null);
          const series = Array.isArray(spec.series) && spec.series.length ? spec.series : (spec.y ? [spec.y] : columns.filter(c => c !== xName).slice(0,3));
          const rawType = String(spec.type || 'line').toLowerCase();
          const type = (rawType === 'area') ? 'line' : (rawType === 'bar_horizontal' || rawType === 'bar-horizontal' || rawType === 'horizontal-bar') ? 'bar' : (rawType === 'pie' || rawType === 'doughnut' || rawType === 'scatter') ? rawType : (rawType === 'bar' ? 'bar' : 'line');
          const stacked = !!spec.stacked;
          const xi = columns.indexOf(xName); if (xi < 0) return;
          const labels = rows.map(r => r[xi]);
          const palette = ['#60a5fa','#34d399','#f472b6','#f59e0b','#a78bfa','#22d3ee'];
          let datasets;
          if (type === 'scatter') {
            const yi = columns.indexOf(spec.y || (columns.find(c => c !== xName) || ''));
            if (yi < 0) return;
            datasets = [{ label: spec.y || 'series', data: rows.map(r => ({ x: Number(r[xi]) || 0, y: Number(r[yi]) || 0 })), borderColor: palette[0], backgroundColor: palette[0] + '88' }];
          } else if (type === 'pie' || type === 'doughnut') {
            const yi = columns.indexOf(spec.y || (columns.find(c => c !== xName) || ''));
            if (yi < 0) return;
            datasets = [{ label: spec.y || 'value', data: rows.map(r => (typeof r[yi] === 'number' ? r[yi] : Number(r[yi]) || 0)), backgroundColor: rows.map((_, i) => palette[i % palette.length]) }];
          } else {
            datasets = series.map((sname, i) => {
              const si = columns.indexOf(sname); if (si < 0) return null;
              const color = palette[i % palette.length];
              const data = rows.map(r => (typeof r[si] === 'number' ? r[si] : Number(r[si]) || 0));
              const base = { label: sname, data, borderColor: color, backgroundColor: color + '88', borderWidth: 2, pointRadius: 0, tension: 0.2 };
              if (rawType === 'area') base.fill = true;
              return base;
            }).filter(Boolean);
          }
          if (!datasets.length || !labels.length) return;
          canvas.style.height = canvas.style.height || '280px';
          if (!canvas.style.width) canvas.style.width = '100%';
          const options = { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: true }, title: { display: !!spec.title, text: spec.title || '' } } };
          if (type === 'pie' || type === 'doughnut') {
            // no scales
          } else if (type === 'scatter') {
            options.scales = { x: { type: 'linear', beginAtZero: true }, y: { type: 'linear', beginAtZero: true } };
          } else {
            options.scales = { x: { stacked }, y: { stacked, beginAtZero: true } };
          }
          if (rawType === 'bar_horizontal' || rawType === 'bar-horizontal' || rawType === 'horizontal-bar') options.indexAxis = 'y';
          t._chart = new Chart(ctx, { type, data: { labels, datasets }, options });
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
      function llmElapsed(t) {
        void tick.value;
        if (!t) return null;
        const s = (typeof t.llmStart === 'number') ? t.llmStart : null;
        const e = (typeof t.llmEnd === 'number') ? t.llmEnd : (s != null ? Date.now() : null);
        if (s == null || e == null) return null;
        return Math.max(0, e - s);
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
            let allTools = [];
            function upsertTools(dst, srcBatch) {
              for (const t of (srcBatch || [])) {
                const i = dst.findIndex(x => x.id === t.id);
                if (i >= 0) {
                  // merge basic fields but keep existing output/start/end if present
                  dst[i].name = t.name || dst[i].name;
                  dst[i].arguments = (t.arguments != null) ? t.arguments : dst[i].arguments;
                  dst[i].title = (t.title != null) ? t.title : dst[i].title;
                } else {
                  dst.push({ ...t });
                }
              }
            }
            function parseArgs(raw) {
              try { return typeof raw === 'string' ? JSON.parse(raw) : raw; } catch { return raw; }
            }
            function parseJSON(raw) {
              try { return typeof raw === 'string' ? JSON.parse(raw) : raw; } catch { return raw; }
            }
            for (const m of list) {
              if (!m) continue;
              if (m.role === 'assistant' && m.tool_calls) {
                const batch = (m.tool_calls || []).map(tc => ({
                  id: tc.id,
                  name: tc.function && tc.function.name,
                  arguments: parseArgs(tc.function && tc.function.arguments),
                  title: (() => { const a = parseArgs(tc.function && tc.function.arguments); return a && a.title; })(),
                  output: undefined,
                  expanded: false,
                  start: null,
                  end: null,
                  thinking: m.thinking || undefined,
                }));
                pendingTools = batch;
                upsertTools(allTools, batch);
                // Skip adding this intermediate assistant message
                continue;
              }
              if (m.role === 'tool') {
                const parsed = parseJSON(m.content);
                if (pendingTools) {
                  const t = pendingTools.find(x => x.id === m.tool_call_id);
                  if (t) { t.output = parsed; if (typeof m.start_ms === 'number') t.start = m.start_ms; if (typeof m.end_ms === 'number') t.end = m.end_ms; }
                }
                if (allTools && allTools.length) {
                  const t2 = allTools.find(x => x.id === m.tool_call_id);
                  if (t2) { t2.output = parsed; if (typeof m.start_ms === 'number') t2.start = m.start_ms; if (typeof m.end_ms === 'number') t2.end = m.end_ms; }
                }
                continue;
              }
              if (m.role === 'user') {
                out.push({ role: 'user', content: m.content || '' });
                continue;
              }
              if (m.role === 'assistant') {
                const amsg = { role: 'assistant', content: m.content || '', renderRaw: false };
                if (m.model) amsg.model = m.model;
                if (m.thinking) { amsg.thinking = m.thinking; amsg.thinkingExpanded = true; }
                const attach = (allTools && allTools.length) ? allTools : pendingTools;
                if (attach && attach.length) {
                  amsg.tools = attach.map(t => ({ ...t }));
                  // If there are display_chart tools, pre-assign chart IDs
                  for (const t of amsg.tools) {
                    if (t && t.name === 'display_chart') {
                      t._chartId = t._chartId || ('chart-' + Math.random().toString(36).slice(2,9));
                      // Keep collapsed on refresh; render when user expands
                      t.expanded = false;
                    }
                  }
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
                allTools = [];
              }
            }
            // If tools existed without a final assistant, show them as a separate assistant block
            const leftover = (allTools && allTools.length) ? allTools : pendingTools;
            if (leftover && leftover.length) {
              const amsg = { role: 'assistant', content: '', tools: leftover.map(t => ({ ...t })), renderRaw: false };
              // Best-effort: show current session model if available
              if (model.value) amsg.model = model.value;
              for (const t of amsg.tools) { if (t && t.name === 'display_chart') { t._chartId = t._chartId || ('chart-' + Math.random().toString(36).slice(2,9)); t.expanded = false; } }
              const sqlTool = amsg.tools.find(t => (t.name === 'display_result' || t.name === 'sql_query') && t.output && t.output.columns && t.output.rows);
              if (sqlTool) { const o = sqlTool.output; amsg.preview = { columns: o.columns, rows: o.rows, rowcount: o.rowcount }; amsg.previewExpanded = false; }
              out.push(amsg);
            }
            messages.value = out;
            await nextTick();
            try {
              // Render any inline charts embedded in assistant content
              messages.value.forEach((m, i) => { if (m.role === 'assistant') renderInlineChartsForMessage(i); });
              // Render charts for any display_chart tools we've auto-expanded
              messages.value.forEach(m => {
                if (m.role === 'assistant' && Array.isArray(m.tools)) {
                  m.tools.forEach(t => { if (t && t.name === 'display_chart' && t.expanded) { try { renderChartForTool(t); } catch {} } });
                }
              });
              // Backup pass after a short delay (in case Chart.js finishes loading later)
              setTimeout(() => {
                try {
                  messages.value.forEach((m, i) => { if (m.role === 'assistant') renderInlineChartsForMessage(i); });
                } catch {}
              }, 400);
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
        const assistantMsg = { role: 'assistant', content: '', renderRaw: false, model: model.value || undefined };
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
          const res = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ chat_id: chatId.value, message: text, show_thinking: !!settings.value.showThinking }), signal: controller.signal });
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
                  const llmStart = (typeof evt.llm_start_ms === 'number') ? evt.llm_start_ms : null;
                  const llmEnd = (typeof evt.llm_end_ms === 'number') ? evt.llm_end_ms : null;
                  if (existing) {
                    existing.name = evt.name;
                    existing.arguments = evt.arguments;
                    existing.title = (evt.arguments && evt.arguments.title) || existing.title;
                    existing.llmStart = llmStart;
                    existing.llmEnd = llmEnd;
                    if (evt.thinking != null) existing.thinking = String(evt.thinking);
                  }
                  else {
                    const item = { id: evt.id, name: evt.name, title: (evt.arguments && evt.arguments.title) || undefined, arguments: evt.arguments, output: undefined, expanded: false, start: Date.now(), end: null, llmStart: llmStart, llmEnd: llmEnd };
                    if (evt.thinking != null) item.thinking = String(evt.thinking);
                    assistantMsg.tools.push(item);
                  }
                  await nextTick();
                } else if (evt.type === 'tool_result') {
                  if (!assistantMsg.tools) assistantMsg.tools = [];
                  const existing = assistantMsg.tools.find(t => t.id === evt.id);
                  const se = (typeof evt.start_ms === 'number') ? evt.start_ms : null;
                  const ee = (typeof evt.end_ms === 'number') ? evt.end_ms : null;
                  if (existing) {
                    existing.output = evt.output;
                    if (se != null) existing.start = se;
                    if (ee != null) existing.end = ee; else existing.end = Date.now();
                  } else {
                    assistantMsg.tools.push({ id: evt.id, name: evt.name, arguments: undefined, output: evt.output, expanded: false, start: (se ?? Date.now()), end: (ee ?? Date.now()) });
                  }
                  if ((evt.name === 'display_result' || evt.name === 'sql_query') && evt.output && evt.output.columns && evt.output.rows) { assistantMsg.preview = { columns: evt.output.columns, rows: evt.output.rows, rowcount: evt.output.rowcount }; assistantMsg.previewExpanded = false; }
                  if (evt.name === 'display_chart') {
                    const tool = existing || (assistantMsg.tools.find(t => t.id === evt.id));
                    if (tool) { tool._chartId = tool._chartId || ('chart-' + Math.random().toString(36).slice(2,9)); if (tool.expanded) { await nextTick(); renderChartForTool(tool); } }
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
          try { const idx = messages.value.lastIndexOf(assistantMsg); const i = idx >= 0 ? idx : (messages.value.length - 1); renderInlineChartsForMessage(i); } catch {}
        }
      }

      async function onSubmit() { const text = (input.value || '').trim(); if (!text || streaming.value) return; input.value = ''; await sendMessage(text); }
      function stopStreaming() { if (controller) controller.abort(); }
      const canRetry = Vue.computed(() => { const lastUser = [...messages.value].reverse().find(m => m.role === 'user'); return !!lastUser && !streaming.value; });
      async function retryLast() { if (streaming.value) return; const lastUser = [...messages.value].reverse().find(m => m.role === 'user'); if (!lastUser) return; await sendMessage(lastUser.content); }

      onMounted(async () => {
        loadSettings();
        // Close settings on Escape
        onKey = (e) => { if (e.key === 'Escape') { showSettings.value = false; modelOpen.value = false; } };
        window.addEventListener('keydown', onKey);
        try { const r = await fetch('/api/meta'); if (r.ok) { const d = await r.json(); model.value = d.model || ''; allowedModels.value = []; } } catch {}
        await refreshModels();
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

      // Models listing from server/provider
      async function refreshModels() {
        loadingModels.value = true;
        try {
          const r = await fetch('/api/models');
          if (r.ok) {
            const d = await r.json();
            const list = Array.isArray(d.models) ? d.models : [];
            if (list.length) allowedModels.value = list;
          }
        } catch {}
        finally { loadingModels.value = false; }
      }
      function filteredModels() {
        const q = (modelQuery.value || '').toLowerCase().trim();
        if (!q) return allowedModels.value;
        return allowedModels.value.filter(m => m.toLowerCase().includes(q));
      }
      function filteredHasExact() { const q = (modelQuery.value || '').trim(); return !!allowedModels.value.find(m => m === q); }
      function pickModel(name) { modelOpen.value = false; modelQuery.value = ''; applyModel(name); }
      function applySearchedModel() { const q = (modelQuery.value || '').trim(); if (q) { pickModel(q); } }

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
        let name = String(newModel || '').trim();
        if (!name) return;
        if (name === '__custom__') {
          const entered = prompt('Enter model ID (e.g., openai/gpt-4o-mini, anthropic/claude-3.5-sonnet):', model.value || '');
          if (!entered) return;
          name = String(entered).trim();
        }
        try {
          await fetch(`/api/sessions/${encodeURIComponent(chatId.value)}`, {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model: name })
          });
          model.value = name;
          toast(`Model set to ${name}`);
        } catch {}
      }

      function previewRowCount(preview) { if (!preview) return 0; if (preview.rowcount !== undefined && preview.rowcount !== null) return preview.rowcount; if (Array.isArray(preview.rows)) return preview.rows.length; return 0; }

      return { chatId, messages, input, inputEl, streaming, model, allowedModels, loadingModels, modelOpen, modelQuery, sidebarOpen, sessions, loadingSessions, loadingChat, showSettings, settings, toasts, newChat, createChat, selectChat, renameChat, deleteChat, onSubmit, stopStreaming, retryLast, canRetry, copyText, renderMarkdown, renderCode, renderSQL, toolSummary, displayTitle, toggleTool, toolError, copyJSON, copyCSV, downloadCSV, previewRowCount, toolElapsed, llmElapsed, formatDuration, turnElapsed, sortedSessions, formatTimeAgo, autosize, saveSettings, toggleRender, applyModel, renderChartForTool, refreshModels, filteredModels, filteredHasExact, pickModel, applySearchedModel };
    }
  }).mount('#app');
})();
