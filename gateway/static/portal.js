'use strict';
let csrf = '';
let generation = 0;
let signedOut = false;
let secretTimer;
const message = document.getElementById('message');
const el = id => document.getElementById(id);
function clearSecret() { clearTimeout(secretTimer); el('secret').value = ''; }
function clearPasswords() { el('password-form').reset(); }
function clearLocal() {
  signedOut = true; ++generation; csrf = '';
  clearPasswords(); el('security').hidden = true;
  clearSecret(); el('secret-dialog').close(); el('profile').textContent = ''; el('keys').replaceChildren();
}
async function api(path, method = 'GET', body) {
  const headers = csrf ? {'X-CSRF-Token': csrf} : {};
  if (body) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, { method, credentials: 'same-origin', cache: 'no-store', headers, body: body ? JSON.stringify(body) : undefined });
  if (!response.ok) throw new Error(response.status === 401 ? 'Sign in to view your access.' : `Request failed (${response.status}).`);
  return response.status === 204 ? null : response.json();
}
async function load() {
  const version = ++generation;
  try {
    const profile = await api('/account/api/profile');
    if (signedOut || version !== generation) return;
    csrf = profile.csrf_token || '';
    el('security').hidden = !csrf;
    el('password-form').hidden = !profile.can_change_password;
    el('password-mfa').hidden = !profile.local_mfa_enrolled;
    el('password-code').required = Boolean(profile.local_mfa_enrolled);
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
      clearPasswords(); el('security').hidden = true;
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
el('cancel-password').addEventListener('click', clearPasswords);
el('revoke-sessions').addEventListener('click', async () => {
  if (!confirm('Sign out your other gateway browsers? API keys and identity-provider sessions are unchanged.')) return;
  const version = generation;
  el('revoke-sessions').disabled = true;
  try {
    await api('/account/api/sessions/revoke-others', 'POST');
    if (!signedOut && version === generation) el('security-message').textContent = 'Other gateway browsers signed out.';
  } catch (_) {
    if (!signedOut && version === generation) el('security-message').textContent = 'Could not confirm session revocation. Try again.';
  } finally { el('revoke-sessions').disabled = false; }
});
el('password-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (el('change-password').disabled || signedOut) return;
  if (el('new-password').value !== el('confirm-password').value) {
    clearPasswords(); el('security-message').textContent = 'New passwords did not match. Please re-enter them.'; return;
  }
  const body = {current_password:el('current-password').value, new_password:el('new-password').value};
  if (!el('password-mfa').hidden) body.code = el('password-code').value;
  clearPasswords();
  el('change-password').disabled = true;
  try {
    await api('/account/api/password', 'POST', body);
    if (!signedOut) {
      clearLocal(); message.textContent = 'Password changed. All gateway browsers signed out. Sign in again with your new password.';
    }
  } catch (_) {
    if (!signedOut) el('security-message').textContent = 'Password change was not confirmed. Check your current password and a fresh authenticator code, or sign in again. If the connection failed, the change may have completed.';
  } finally { el('change-password').disabled = false; }
});
api('/auth/local/options').then(options => { el('local-login').hidden = !options.enabled; }).catch(() => {});
window.addEventListener('pagehide', clearLocal);
load();
