/* Self-contained floating AI chat widget for the Clinical Research Platform.
 * Drop into any served page with:  <script src="/ui/chat-widget.js"></script>
 * Talks to POST /chat/stream (SSE) with graceful fallback to POST /chat/.
 * No dependencies; styles itself inline so it works regardless of the host
 * page's CSS.
 */
(function () {
  if (window.__crpChatWidget) return;
  window.__crpChatWidget = true;

  var history = [];

  function el(tag, css, text) {
    var e = document.createElement(tag);
    if (css) e.style.cssText = css;
    if (text != null) e.textContent = text;
    return e;
  }

  function build() {
    // Launcher button
    var btn = el('button', [
      'position:fixed', 'right:20px', 'bottom:20px', 'z-index:9999',
      'background:#2563eb', 'color:#fff', 'border:none', 'border-radius:9999px',
      'padding:12px 18px', 'font:600 14px system-ui,sans-serif',
      'box-shadow:0 4px 14px rgba(0,0,0,.25)', 'cursor:pointer'
    ].join(';'), 'Ask AI');

    // Panel
    var panel = el('div', [
      'position:fixed', 'right:20px', 'bottom:20px', 'z-index:10000',
      'width:360px', 'max-width:calc(100vw - 40px)', 'height:520px',
      'max-height:calc(100vh - 40px)', 'display:none', 'flex-direction:column',
      'background:#fff', 'border:1px solid #e2e8f0', 'border-radius:14px',
      'box-shadow:0 10px 40px rgba(0,0,0,.3)', 'overflow:hidden',
      'font:14px system-ui,sans-serif', 'color:#1e293b'
    ].join(';'));

    // Header
    var header = el('div', 'display:flex;align-items:center;gap:8px;padding:10px 12px;background:#2563eb;color:#fff;');
    header.appendChild(el('div', 'font-weight:700;flex:1', 'AI Assistant'));
    var close = el('button', 'background:transparent;border:none;color:#fff;font-size:20px;cursor:pointer;line-height:1', '×');
    header.appendChild(close);

    // Subject row
    var subjRow = el('div', 'display:flex;align-items:center;gap:6px;padding:8px 12px;border-bottom:1px solid #eef2f7;background:#f8fafc;');
    subjRow.appendChild(el('label', 'font-size:12px;color:#64748b', 'Subject:'));
    var subjInput = el('input');
    subjInput.placeholder = 'e.g. SUBJ-0012 (optional)';
    subjInput.style.cssText = 'flex:1;border:1px solid #cbd5e1;border-radius:6px;padding:3px 6px;font-size:12px;';
    // Pre-fill from a subject/voucher field on the host page if present.
    var pageSubj = document.getElementById('subject') || document.getElementById('voucher');
    if (pageSubj && pageSubj.value) subjInput.value = pageSubj.value.trim();
    subjRow.appendChild(subjInput);

    // Log
    var log = el('div', 'flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px;background:#f8fafc;');
    var hint = el('div', 'font-size:12px;color:#94a3b8', "Ask about a subject’s protocol visits, compliance, and transport trips. I remember what you teach me.");
    log.appendChild(hint);

    // Input row
    var form = el('form', 'display:flex;gap:6px;padding:10px;border-top:1px solid #eef2f7;');
    var input = el('input');
    input.placeholder = 'Ask something…';
    input.style.cssText = 'flex:1;border:1px solid #cbd5e1;border-radius:8px;padding:8px;';
    var send = el('button', 'background:#2563eb;color:#fff;border:none;border-radius:8px;padding:8px 14px;font-weight:600;cursor:pointer;', 'Send');
    send.type = 'submit';
    form.appendChild(input);
    form.appendChild(send);

    panel.appendChild(header);
    panel.appendChild(subjRow);
    panel.appendChild(log);
    panel.appendChild(form);
    document.body.appendChild(btn);
    document.body.appendChild(panel);

    function bubble(role, text) {
      var wrap = el('div', 'display:flex;' + (role === 'user' ? 'justify-content:flex-end' : 'justify-content:flex-start'));
      var b = el('div', [
        'max-width:85%', 'padding:8px 10px', 'border-radius:10px',
        'white-space:pre-wrap', 'word-break:break-word',
        role === 'user' ? 'background:#2563eb;color:#fff' : 'background:#e2e8f0;color:#1e293b'
      ].join(';'), text);
      wrap.appendChild(b);
      log.appendChild(wrap);
      log.scrollTop = log.scrollHeight;
      return b;
    }

    function appendProvenance(bubbleEl, sources, toolCalls) {
      var items = (sources && sources.length)
        ? sources
        : (toolCalls && toolCalls.length ? toolCalls.map(function (t) { return t.summary || t.name; }) : []);
      if (!items.length) return;
      var meta = el('div', 'font-size:11px;color:#94a3b8;margin-top:2px;',
        'Sources: ' + items.join(' · '));
      bubbleEl.parentElement.appendChild(meta);
    }

    function sendStreaming(payload, thinkingBubble, onDone, onError) {
      fetch('/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      }).then(function (res) {
        if (!res.ok) {
          // Non-2xx before streaming starts (e.g. 503 no-key)
          return res.json().catch(function () { return {}; }).then(function (d) {
            var err = new Error(d.detail || ('HTTP ' + res.status));
            err.status = res.status;
            throw err;
          });
        }
        var reader = res.body.getReader();
        var decoder = new TextDecoder();
        var buffer = '';
        var replyText = '';

        thinkingBubble.textContent = '';

        function pump() {
          return reader.read().then(function (chunk) {
            if (chunk.done) {
              onDone({ reply: replyText, meta: null });
              return;
            }
            buffer += decoder.decode(chunk.value, { stream: true });
            var parts = buffer.split('\n\n');
            buffer = parts.pop();
            for (var i = 0; i < parts.length; i++) {
              var line = parts[i].trim();
              if (line.indexOf('data:') !== 0) continue;
              var json_str = line.slice(5).trim();
              var evt;
              try { evt = JSON.parse(json_str); } catch (e) { continue; }
              if (evt.error) {
                onError(new Error(evt.error));
                return;
              } else if (evt.done) {
                onDone({ reply: replyText, meta: evt });
                return;
              } else if (typeof evt.token === 'string') {
                replyText += evt.token;
                thinkingBubble.textContent = replyText;
                log.scrollTop = log.scrollHeight;
              }
            }
            return pump();
          });
        }
        return pump();
      }).catch(onError);
    }

    btn.onclick = function () { panel.style.display = 'flex'; btn.style.display = 'none'; input.focus(); };
    close.onclick = function () { panel.style.display = 'none'; btn.style.display = 'block'; };

    form.onsubmit = function (e) {
      e.preventDefault();
      var text = input.value.trim();
      if (!text) return;
      input.value = '';
      send.disabled = true;
      bubble('user', text);
      history.push({ role: 'user', content: text });
      var thinking = bubble('assistant', '…');
      var payload = { messages: history, subject: subjInput.value.trim() || null };

      function finish() {
        send.disabled = false;
        input.focus();
      }

      function applyResult(reply, toolCalls, sources) {
        if (thinking.textContent !== reply) {
          thinking.textContent = reply || '(no reply)';
        }
        history.push({ role: 'assistant', content: reply || '' });
        appendProvenance(thinking, sources, toolCalls);
        finish();
      }

      function fallbackToNonStreaming() {
        thinking.textContent = '…';
        fetch('/chat/', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        }).then(function (r) {
          return r.json().then(function (d) { return { ok: r.ok, status: r.status, d: d }; });
        }).then(function (res) {
          if (!res.ok) {
            thinking.textContent = '⚠ ' + (res.d.detail || ('Error ' + res.status));
            thinking.style.background = '#fef3c7';
            thinking.style.color = '#92400e';
            finish();
          } else {
            applyResult(res.d.reply || '', res.d.tool_calls || [], res.d.sources || []);
          }
        }).catch(function (err) {
          thinking.textContent = '⚠ Network error: ' + err;
          finish();
        });
      }

      sendStreaming(payload, thinking,
        function onDone(result) {
          var meta = result.meta || {};
          applyResult(result.reply || '', meta.tool_calls || [], meta.sources || []);
        },
        function onError(err) {
          // Fall back to non-streaming on any stream error
          fallbackToNonStreaming();
        }
      );
    };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', build);
  } else {
    build();
  }
})();
