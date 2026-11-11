'use strict';
let csrf = '';
const message = document.getElementById('message');
async function api(path, method = 'GET') {
  const response = await fetch(path, { method, credentials: 'same-origin', headers: csrf ? {'X-CSRF-Token': csrf} : {} });
  if (!response.ok) throw new Error(response.status === 401 ? 'Sign in to view your access.' : `Request failed (${response.status}).`);
  return response.status === 204 ? null : response.json();
}
async function load() {
  try {
    const profile = await api('/account/api/profile');
    csrf = profile.csrf_token || '';
    const visible = {...profile}; delete visible.csrf_token;
    document.getElementById('profile').textContent = JSON.stringify(visible, null, 2);
    const container = document.getElementById('keys'); container.replaceChildren();
    for (const key of await api('/account/api/keys')) {
      const row = document.createElement('div'); row.className = 'card';
      const label = document.createElement('p');
      label.textContent = `${key.name} · ${key.prefix}… · expires ${key.expires_at} · ${key.revoked ? 'revoked' : 'active'}`;
      row.append(label);
      if (!key.revoked) for (const action of ['rotate', 'revoke']) {
        const button = document.createElement('button'); button.textContent = action === 'rotate' ? 'Rotate' : 'Revoke';
        button.addEventListener('click', async () => {
          if (!confirm(`${action === 'rotate' ? 'Replace' : 'Revoke'} this key? Existing clients will lose access immediately.`)) return;
          button.disabled = true;
          try {
            const result = await api(`/account/api/keys/${encodeURIComponent(key.id)}/${action}`, 'POST');
            if (result.key) { document.getElementById('secret').value = result.key; document.getElementById('secret-dialog').showModal(); }
            await load();
          } catch (error) { message.textContent = error.message; button.disabled = false; }
        }); row.append(button);
      }
      container.append(row);
    }
    message.textContent = '';
  } catch (error) { message.textContent = error.message; }
}
document.getElementById('close-secret').addEventListener('click', () => { document.getElementById('secret').value = ''; document.getElementById('secret-dialog').close(); });
document.getElementById('secret-dialog').addEventListener('close', () => { document.getElementById('secret').value = ''; });
document.getElementById('logout').addEventListener('click', async () => { await fetch('/auth/logout', {method:'POST', credentials:'same-origin', headers:{'X-CSRF-Token':csrf}}); location.reload(); });
load();
