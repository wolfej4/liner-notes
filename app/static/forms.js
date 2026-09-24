// Every change is sent as JSON with the X-Liner-Notes header, which the server requires on all
// POST/DELETE requests. That header can't be added by another site, which blocks cross-site forgery.
(() => {
  const flash = document.getElementById('flash');

  function showFlash(message, link) {
    if (!flash) return;
    flash.hidden = false;
    flash.innerHTML = '';
    const close = document.createElement('button');
    close.className = 'close'; close.type = 'button'; close.textContent = 'Close';
    close.onclick = () => { flash.hidden = true; };
    const p = document.createElement('div');
    p.textContent = message;
    flash.append(close, p);
    if (link) flash.append(copyRow(link));
  }

  function copyRow(link) {
    const row = document.createElement('div');
    row.className = 'copyrow';
    const input = document.createElement('input');
    input.type = 'text'; input.readOnly = true; input.value = link;
    input.onfocus = () => input.select();
    const btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'btn small'; btn.textContent = 'Copy link';
    btn.onclick = async () => {
      try { await navigator.clipboard.writeText(link); btn.textContent = 'Copied'; }
      catch (e) { input.select(); }
    };
    row.append(input, btn);
    return row;
  }

  async function send(url, method, body) {
    const r = await fetch(url, {
      method, credentials: 'same-origin',
      headers: Object.assign({ 'X-Liner-Notes': '1' }, body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      body: body !== undefined ? JSON.stringify(body) : undefined
    });
    let data = {};
    try { data = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error(data.detail || `Something went wrong (error ${r.status}).`);
    return data;
  }

  function after(data, reload) {
    if (data.redirect) { location.href = data.redirect; return true; }
    if (reload && !data.link) { location.reload(); return true; }
    return false;
  }

  document.addEventListener('submit', async e => {
    const form = e.target.closest('form[data-api]');
    if (!form) return;
    e.preventDefault();
    const msg = form.querySelector('.form-msg');
    const body = {};
    for (const el of form.elements) {
      if (!el.name || el.disabled) continue;
      if (el.type === 'checkbox') body[el.name] = el.checked;
      else if (el.type === 'radio') { if (el.checked) body[el.name] = el.value; }
      else body[el.name] = el.value;
    }
    const submit = form.querySelector('[type=submit]');
    if (submit) submit.disabled = true;
    if (msg) { msg.textContent = ''; msg.className = 'form-msg'; }
    try {
      const data = await send(form.dataset.api, form.dataset.method || 'POST', body);
      if (after(data, form.hasAttribute('data-reload'))) return;
      if (form.hasAttribute('data-clear')) form.querySelectorAll('input[type=password]').forEach(i => { i.value = ''; });
      if (data.link) showFlash(data.message || 'Done.', data.link);
      else if (msg) { msg.textContent = data.message || 'Saved.'; msg.className = 'form-msg ok'; }
    } catch (err) {
      if (msg) { msg.textContent = err.message; msg.className = 'form-msg err'; }
      else showFlash(err.message);
    } finally {
      if (submit) submit.disabled = false;
    }
  });

  document.addEventListener('click', async e => {
    const btn = e.target.closest('button[data-post]');
    if (!btn) return;
    if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
    btn.disabled = true;
    try {
      const body = btn.dataset.body ? JSON.parse(btn.dataset.body) : {};
      const data = await send(btn.dataset.post, btn.dataset.method || 'POST', body);
      if (after(data, btn.hasAttribute('data-reload'))) return;
      showFlash(data.message || 'Done.', data.link);
    } catch (err) {
      showFlash(err.message);
    } finally {
      btn.disabled = false;
    }
  });
})();

// Signing out from any page also forgets the dashboard's copy of the history in this browser.
document.addEventListener('click', e => {
  if (e.target.closest('button[data-post="/api/logout"]')) {
    try {
      const req = indexedDB.open('liner-notes', 1);
      req.onupgradeneeded = () => req.result.createObjectStore('kv');
      req.onsuccess = () => {
        try { req.result.transaction('kv', 'readwrite').objectStore('kv').delete('server-cache'); } catch (err) {}
      };
    } catch (err) {}
  }
}, true);
