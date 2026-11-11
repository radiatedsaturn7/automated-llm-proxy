'use strict';
let csrf = '';
let generation = 0;
let signedOut = false;
let secretTimer;
const message = document.getElementById('message');
const el = id => document.getElementById(id);
function clearSecret() { clearTimeout(secretTimer); el('secret').value = ''; }
function clearLocal() {
  signedOut = true; ++generation; csrf = '';
  clearSecret(); el('secret-dialog').close(); el('profile').textContent = ''; el('keys').replaceChildren();
}
async function api(path, method = 'GET') {
  const response = await fetch(path, { method, credentials: 'same-origin', cache: 'no-store', headers: csrf ? {'X-CSRF-Token': csrf} : {} });
  if (!response.ok) throw new Error(response.status === 401 ? 'Sign in to view your access.' : `Request failed (${response.status}).`);
  return response.status === 204 ? null : response.json();
}
async function load() {
  const version = ++generation;
  try {
    const profile = await api('/account/api/profile');
    if (signedOut || version !== generation) return;
    csrf = profile.csrf_token || '';
    const visible = {...profile}; delete visible.csrf_token;
    el('profile').textContent = JSON.stringify(visible, null, 2);
    const keys = await api('/account/api/keys');
    if (signedOut || version !== generation) return;
    const container = el('keys'); container.replaceChildren();
    for (const key of keys) {
      const row = document.createElement('div'); row.className = 'card';
      const label = document.createElement('p');
      const expired = !Number.isFinite(Date.parse(key.expires_at)) || Date.parse(key.expires_at) <= Date.now();
      label.textContent = `${key.name} · ${key.prefix}… · expires ${key.expires_at} · ${key.revoked ? 'revoked' : expired ? 'expired' : 'active'}`;
      row.append(label);
      if (!key.revoked) for (const action of (expired ? ['revoke'] : ['rotate', 'revoke'])) {
        const button = document.createElement('button'); button.textContent = action === 'rotate' ? 'Rotate' : 'Revoke';
        button.addEventListener('click', async () => {
          if (!confirm(`${action === 'rotate' ? 'Replace' : 'Revoke'} this key? Existing clients will lose access immediately.`)) return;
          const actionGeneration = generation; button.disabled = true;
          try {
            const result = await api(`/account/api/keys/${encodeURIComponent(key.id)}/${action}`, 'POST');
            if (signedOut || actionGeneration !== generation) return;
            if (result.key) {
              clearSecret(); el('secret').value = result.key; el('secret-dialog').showModal();
              secretTimer = setTimeout(() => { clearSecret(); el('secret-dialog').close(); }, 120000);
            }
            await load();
          } catch (error) { if (!signedOut && actionGeneration === generation) { message.textContent = error.message; button.disabled = false; } }
        }); row.append(button);
      }
      container.append(row);
    }
    message.textContent = '';
  } catch (error) {
    if (!signedOut && version === generation) {
      csrf = ''; el('profile').textContent = ''; el('keys').replaceChildren(); clearSecret(); el('secret-dialog').close();
      message.textContent = error.message;
    }
  }
}
el('close-secret').addEventListener('click', () => { clearSecret(); el('secret-dialog').close(); });
el('secret-dialog').addEventListener('close', clearSecret);
el('secret-dialog').addEventListener('cancel', clearSecret);
el('logout').addEventListener('click', async () => {
  const token = csrf; clearLocal(); message.textContent = 'Signing out…';
  try {
    const response = await fetch('/auth/logout', {method:'POST', credentials:'same-origin', cache:'no-store', headers:{'X-CSRF-Token':token}});
    if (!response.ok) throw new Error('Sign-out failed');
    message.textContent = 'Signed out. Local credentials cleared.';
  } catch (_) { message.textContent = 'Local credentials cleared. Server sign-out was not confirmed; close this tab and end the session at your identity provider.'; }
});
window.addEventListener('pagehide', clearLocal);
load();
