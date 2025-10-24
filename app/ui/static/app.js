(() => {
  const { createApp, ref, onMounted, onUnmounted, nextTick } = Vue;

  marked.setOptions({
    mangle: false,
    headerIds: false,
    highlight: (code, lang) => {
      try {
        return hljs.highlightAuto(code, lang ? [lang] : undefined).value;
      } catch { return code; }
    }
  });

  function sanitize(html) { return DOMPurify.sanitize(html); }

  createApp({
    setup() {
      const chatId = ref(null);
      const messages = ref([]);
      const input = ref('');
      const model = ref('');
      const streaming = ref(false);
      let controller = null;
      const tick = ref(0);
      let tickTimer = null;
      const turnStart = ref(null);

      async function ensureChat() {
        if (!chatId.value) {
          const res = await fetch('/api/new_chat', { method: 'POST' });
          const data = await res.json();
          chatId.value = data.chat_id;
        }
      }

      async function newChat() {
        chatId.value = null;
        messages.value = [];
        input.value = '';
        await ensureChat();
      }

      function scrollToBottom() {
        const el = document.getElementById('chat');
        if (el) el.scrollTop = el.scrollHeight;
      }

      function renderMarkdown(text) {
        return sanitize(marked.parse(text || ''));
      }

      function renderCode(obj) {
        try {
          const json = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2);
          const highlighted = hljs.highlight(json, { language: 'json' }).value;
          return sanitize(highlighted);
        } catch (e) {
          const str = (obj == null) ? '' : String(obj);
          return sanitize(str);
        }
      }

      // Pretty-print SQL for readability in the tool panel
      function formatSQL(sql) {
        if (!sql || typeof sql !== 'string') return '';
        let s = sql.trim().replace(/\s+/g, ' ');
        const clauses = [
          'SELECT','FROM','LEFT JOIN','RIGHT JOIN','INNER JOIN','OUTER JOIN','JOIN','WHERE','GROUP BY','HAVING','ORDER BY','LIMIT','OFFSET','UNION ALL','UNION','WITH','ON','AND','OR'
        ];
        for (const c of clauses) {
          const re = new RegExp(`\\s+(${c.replace(/ /g,'\\s+')})\\s+`, 'gi');
          s = s.replace(re, (m,p1) => `\n${p1.toUpperCase()} `);
        }
        s = s.replace(/,\s*/g, ',\n  ');
        const lines = s.split(/\n+/);
        const out = [];
        let indent = 0;
        const base0 = /^(SELECT|FROM|WHERE|GROUP BY|HAVING|ORDER BY|LIMIT|OFFSET|WITH|UNION|UNION ALL)\b/i;
        const base1 = /^(AND|OR|JOIN|LEFT JOIN|RIGHT JOIN|INNER JOIN|OUTER JOIN|ON)\b/i;
        for (let line of lines) {
          const open = (line.match(/\(/g) || []).length;
          const close = (line.match(/\)/g) || []).length;
          if (close > open && indent > 0) indent -= (close - open);
          const pad = base0.test(line) ? 0 : (base1.test(line) ? 2 : Math.max(indent, 0));
          out.push(' '.repeat(pad) + line.trim());
          if (open > close) indent += (open - close);
        }
        return out.join('\n');
      }

      function renderSQL(sql) {
        try {
          const formatted = formatSQL(sql);
          const highlighted = hljs.highlight(formatted || '', { language: 'sql' }).value;
          return sanitize(highlighted);
        } catch (e) {
          return sanitize(sql || '');
        }
      }

      function copyText(text) {
        navigator.clipboard.writeText(text || '');
      }

      function toolElapsed(t) {
        // Reference tick to make this reactive on interval updates
        void tick.value;
        const start = t.start || Date.now();
        const end = (t.output && t.end) ? t.end : Date.now();
        return Math.max(0, end - start);
      }

      function formatDuration(ms) {
        if (ms == null) return '';
        if (ms < 1000) return `${Math.round(ms)} ms`;
        const s = ms / 1000;
        if (s < 10) return `${s.toFixed(2)} s`;
        if (s < 60) return `${s.toFixed(1)} s`;
        const m = Math.floor(s / 60);
        const rem = Math.round(s - m * 60);
        return `${m}m ${rem}s`;
      }

      function turnElapsed() {
        // Reference tick to make this reactive on interval updates
        void tick.value;
        if (!streaming.value || !turnStart.value) return 0;
        return Date.now() - turnStart.value;
      }

      async function sendMessage(text) {
        await ensureChat();
        const userMsg = { role: 'user', content: text };
        messages.value.push(userMsg);
        const assistantMsg = { role: 'assistant', content: '' };
        messages.value.push(assistantMsg);
        await nextTick();
        scrollToBottom();

        controller = new AbortController();
        streaming.value = true;
        turnStart.value = Date.now();
        if (!tickTimer) {
          tickTimer = setInterval(() => { tick.value = (tick.value + 1) | 0; }, 100);
        }
        try {
          const res = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ chat_id: chatId.value, message: text }),
            signal: controller.signal,
          });

          const reader = res.body
            .pipeThrough(new TextDecoderStream())
            .getReader();

          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            for (const line of value.split('\n')) {
              if (!line.trim()) continue;
              try {
                const evt = JSON.parse(line);
                if (evt.type === 'tool_call') {
                  if (!assistantMsg.tools) assistantMsg.tools = [];
                  const existing = assistantMsg.tools.find(t => t.id === evt.id);
                  if (existing) {
                    existing.name = evt.name; existing.arguments = evt.arguments;
                  } else {
                    assistantMsg.tools.push({ id: evt.id, name: evt.name, arguments: evt.arguments, output: undefined, expanded: false, start: Date.now(), end: null });
                  }
                  await nextTick();
                } else if (evt.type === 'tool_result') {
                  if (!assistantMsg.tools) assistantMsg.tools = [];
                  const existing = assistantMsg.tools.find(t => t.id === evt.id);
                  if (existing) {
                    existing.output = evt.output; existing.end = Date.now();
                  } else {
                    assistantMsg.tools.push({ id: evt.id, name: evt.name, arguments: undefined, output: evt.output, expanded: false, start: Date.now(), end: Date.now() });
                  }
                  // If this is an SQL result, attach a preview for quick viewing
                  if (evt.name === 'sql_query' && evt.output && evt.output.columns && evt.output.rows) {
                    assistantMsg.preview = { columns: evt.output.columns, rows: evt.output.rows, rowcount: evt.output.rowcount };
                    assistantMsg.previewExpanded = false;
                  }
                  await nextTick();
                } else if (evt.tools) {
                  // Back-compat: single aggregate tools event
                  assistantMsg.tools = evt.tools.map(t => ({ ...t, expanded: false }));
                  await nextTick();
                } else if (evt.chunk) {
                  assistantMsg.content += (assistantMsg.content ? ' ' : '') + evt.chunk;
                  await nextTick();
                  scrollToBottom();
                } else if (evt.done) {
                  // no-op
                }
              } catch {}
            }
          }
        } catch (e) {
          if (e.name !== 'AbortError') {
            assistantMsg.content += '\n\n_(stream error)_';
          }
        } finally {
          streaming.value = false;
          controller = null;
          turnStart.value = null;
        }
      }

      async function onSubmit() {
        const text = (input.value || '').trim();
        if (!text || streaming.value) return;
        input.value = '';
        await sendMessage(text);
      }

      onMounted(async () => {
        await ensureChat();
      });
      onUnmounted(() => { if (tickTimer) clearInterval(tickTimer); });

      // Tool UI helpers
      function toolSummary(t) {
        try {
          if (t.name === 'sql_query' && t.output) {
            const rc = t.output.rowcount ?? (t.output.rows ? t.output.rows.length : undefined);
            const cc = t.output.columns ? t.output.columns.length : undefined;
            const parts = [];
            if (rc != null) parts.push(`${rc} rows`);
            if (cc != null) parts.push(`${cc} cols`);
            return parts.join(', ') || 'query';
          }
          if (t.name === 'sql_schema' && t.output) {
            const tables = Array.isArray(t.output.tables) ? t.output.tables.length : (t.output.tables ? Object.keys(t.output.tables).length : 0);
            return `${tables} tables`;
          }
          if (t.name === 'list_files' && t.output) {
            return `${(t.output.files||[]).length} files`;
          }
          if (t.name === 'search_files' && t.output) {
            return `${(t.output.hits||[]).length} matches for "${(t.output.query||'').toString().slice(0,32)}"`;
          }
          if (t.name === 'read_file' && t.output) {
            const p = t.output.path || '';
            const len = (t.output.content||'').length;
            return `${p} (${len} chars)`;
          }
          if (t.name === 'write_file' && t.output) {
            const p = t.output.path || '';
            const b = t.output.bytes;
            return `wrote ${b} bytes to ${p}`;
          }
        } catch {}
        return 'details';
      }

      function toggleTool(t) { t.expanded = !t.expanded; }
      function copyJSON(obj) {
        try {
          const json = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2);
          navigator.clipboard.writeText(json);
        } catch { navigator.clipboard.writeText(String(obj ?? '')); }
      }

      function copyCSV(preview) {
        try {
          const cols = preview.columns || [];
          const rows = preview.rows || [];
          const escape = (v) => {
            if (v == null) return '';
            const s = String(v);
            return /[",\n]/.test(s) ? '"' + s.replace(/\"/g, '\"\"') + '"' : s;
          };
          const header = cols.map(escape).join(',');
          const body = rows.map(r => cols.map((_, i) => escape(r[i])).join(',')).join('\n');
          const csv = header + '\n' + body;
          navigator.clipboard.writeText(csv);
        } catch {}
      }

      function previewRowCount(preview) {
        if (!preview) return 0;
        if (preview.rowcount !== undefined && preview.rowcount !== null) return preview.rowcount;
        if (Array.isArray(preview.rows)) return preview.rows.length;
        return 0;
      }

      return { chatId, messages, input, streaming, model, newChat, onSubmit, copyText, renderMarkdown, renderCode, renderSQL, toolSummary, toggleTool, copyJSON, copyCSV, previewRowCount, toolElapsed, formatDuration, turnElapsed };
    }
  }).mount('#app');
})();
