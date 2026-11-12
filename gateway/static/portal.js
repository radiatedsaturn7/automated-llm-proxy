'use strict';
let csrf = '';
let generation = 0;
let signedOut = false;
let secretTimer;
let allowance = null;
let resetOperation = null;
let resetInFlight = false;
let allowanceGeneration = 0;
let usageGeneration = 0;
let usageCursor = null;
let usageBusy = false;
let keyPolicy = null;
let keyPolicyGeneration = 0;
let keyOperation = null;
let inaccessibleKey = null;
let keyIssuanceBusy = false;
let keyIssuanceEpoch = 0;
const message = document.getElementById('message');
const el = id => document.getElementById(id);
function clearSecret() { clearTimeout(secretTimer); el('secret').value = ''; }
function clearPasswords() { el('password-form').reset(); }
function clearLocal() {
  signedOut = true; ++generation; csrf = '';
  ++allowanceGeneration; allowance = null; resetOperation = null; el('allowance-panel').hidden = true; el('allowance-summary').textContent = ''; el('allowance-message').textContent = '';
  clearUsage(); clearKeyIssuance();
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
    if (csrf) { loadAllowance(); loadUsage(); loadKeyPolicy(); }
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
      ++allowanceGeneration; allowance = null; el('allowance-panel').hidden = true;
      clearUsage(); clearKeyIssuance();
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
function renderAllowance() {
  el('refresh-allowance').disabled = resetInFlight;
  el('allowance-panel').hidden = !allowance?.enabled;
  if (!allowance?.enabled) return;
  el('allowance-summary').textContent = `${allowance.requests_remaining} of ${allowance.daily_request_limit} requests remaining today (UTC). ${allowance.resets_remaining} of ${allowance.monthly_reset_limit} reset credits remaining this month. Daily renewal: ${allowance.allowance_renews_at}. Credits renew: ${allowance.resets_renew_at}.`;
  el('reset-usage').textContent = resetOperation ? 'Retry pending reset' : 'Use one reset credit';
  el('reset-usage').disabled = resetInFlight || (!resetOperation && (allowance.requests_remaining !== 0 || allowance.resets_remaining < 1));
}
async function loadAllowance() {
  if (resetInFlight || signedOut) return;
  const version = ++allowanceGeneration;
  try {
    const result = await api('/account/api/usage-allowance');
    if (signedOut || version !== allowanceGeneration) return;
    allowance = result; renderAllowance();
  } catch (_) {
    if (!signedOut && version === allowanceGeneration) {
      el('reset-usage').disabled = true;
      el('allowance-message').textContent = 'Allowance unavailable. Refresh to try again.';
    }
  }
}
el('refresh-allowance').addEventListener('click', loadAllowance);
el('reset-usage').addEventListener('click', async () => {
  if (signedOut || resetInFlight || !allowance?.enabled) return;
  if (!resetOperation) {
    if (allowance.requests_remaining !== 0 || allowance.resets_remaining < 1) return;
    if (!confirm('Use one reset credit to replenish today’s request allowance? Provider calls still cost money, and hard spending/rate limits remain unchanged.')) return;
    resetOperation = {operation_id:crypto.randomUUID(), expected_day:allowance.day};
  }
  resetInFlight = true; ++allowanceGeneration; renderAllowance();
  try {
    const response = await fetch('/account/api/reset-usage', {method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(resetOperation)});
    if (signedOut) return;
    if (!response.ok) {
      // Only an endpoint-specific conflict resolves this pending operation.
      // Overload/auth/proxy errors on a retry cannot tell us whether an earlier
      // attempt committed, so retain its ID rather than spending another credit.
      if (response.status === 409) {
        const error = await response.json().catch(() => null);
        if (['Reset operation was already used for another day', 'Allowance day changed; refresh before resetting', 'No usage reset available', 'Request allowance is not exhausted'].includes(error?.detail)) resetOperation = null;
      }
      throw new Error('Reset not confirmed');
    }
    const result = await response.json();
    if (signedOut) return;
    ++allowanceGeneration; allowance = result; resetOperation = null;
    el('allowance-message').textContent = result.replayed ? 'Earlier reset confirmed. Current usage is shown.' : 'Request allowance replenished. Spending and rate limits are unchanged.';
  } catch (_) {
    if (!signedOut) el('allowance-message').textContent = resetOperation ? 'Reset outcome uncertain. Retry pending reset to confirm the same operation without spending another credit. Do not start a new reset in another tab.' : 'Reset unavailable or allowance changed. Refresh before trying again.';
  } finally {
    resetInFlight = false;
    if (!signedOut) renderAllowance();
  }
});
function clearUsage() {
  ++usageGeneration; usageCursor = null; usageBusy = false;
  el('usage-panel').hidden = true; el('usage-rows').replaceChildren();
  el('usage-window').textContent = ''; el('usage-message').textContent = '';
  el('more-usage').hidden = true; el('refresh-usage').disabled = false;
}
async function loadUsage(next = false) {
  if (signedOut || !csrf || (next && (usageBusy || !usageCursor))) return;
  const version = ++usageGeneration;
  const cursor = next ? usageCursor : null;
  usageBusy = true; el('usage-panel').hidden = false;
  el('more-usage').disabled = true; el('refresh-usage').disabled = true;
  el('usage-message').textContent = 'Loading usage…';
  // Clear old rows when changing windows, never mix pages from distinct queries.
  if (!next) { usageCursor = null; el('usage-rows').replaceChildren(); el('usage-window').textContent = ''; el('more-usage').hidden = true; }
  try {
    const data = await api('/account/api/usage?limit=50' + (cursor ? '&cursor=' + encodeURIComponent(cursor) : ''));
    if (signedOut || version !== usageGeneration) return;
    const rows = document.createDocumentFragment();
    for (const item of data.records) {
      const row = document.createElement('tr');
      const time = new Date(item.created_at);
      const values = [Number.isFinite(time.getTime()) ? time.toISOString() : 'Unknown', item.model,
        item.input_tokens, item.output_tokens, (item.cost_micros / 1000000).toFixed(6), item.estimated ? 'Estimated' : 'Reported'];
      for (const value of values) { const cell = document.createElement('td'); cell.textContent = String(value); row.append(cell); }
      rows.append(row);
    }
    // Replace rather than accumulate an unbounded DOM/history on long sessions.
    el('usage-rows').replaceChildren(rows);
    usageCursor = data.next_cursor;
    el('usage-window').textContent = `Window (UTC): ${data.since} to ${data.until}. New settlements appear after Refresh.`;
    el('usage-message').textContent = data.records.length ? `${data.records.length} settled requests on this page.` : 'No settled requests in this window.';
    el('more-usage').hidden = !usageCursor;
  } catch (_) {
    if (!signedOut && version === usageGeneration) el('usage-message').textContent = 'Usage unavailable. Refresh or retry the next page.';
  } finally {
    if (!signedOut && version === usageGeneration) {
      usageBusy = false; el('more-usage').disabled = false; el('refresh-usage').disabled = false;
    }
  }
}
el('refresh-usage').addEventListener('click', () => loadUsage());
el('more-usage').addEventListener('click', () => loadUsage(true));

function clearKeyIssuance() {
  ++keyIssuanceEpoch; ++keyPolicyGeneration; keyPolicy = null; keyOperation = null; inaccessibleKey = null;
  el('key-issuance').hidden = true; el('create-key-form').reset();
  el('key-policy-summary').textContent = ''; el('key-issuance-message').textContent = '';
  el('recover-key').hidden = true;
}
function renderKeyPolicy() {
  el('key-issuance').hidden = !keyPolicy?.enabled && !keyOperation && !inaccessibleKey;
  el('key-name').disabled = keyIssuanceBusy || Boolean(keyOperation) || Boolean(inaccessibleKey);
  el('create-key').textContent = keyOperation ? 'Retry pending key creation' : 'Create API key';
  el('create-key').disabled = keyIssuanceBusy || Boolean(inaccessibleKey) || (!keyOperation && (!keyPolicy?.enabled || keyPolicy.can_create === false));
  el('recover-key').hidden = !inaccessibleKey;
  el('recover-key').disabled = keyIssuanceBusy;
  if (keyPolicy?.enabled && keyPolicy.can_create === false && !keyOperation && !inaccessibleKey) el('key-issuance-message').textContent = keyPolicy.disabled_reason || 'Key creation is currently unavailable under your policy.';
  if (keyPolicy?.enabled) el('key-policy-summary').textContent = `Models: ${keyPolicy.allowed_models.join(', ')}. Permissions: ${keyPolicy.permissions.join(', ')}. Expires after ${keyPolicy.expires_in_days} days. ${keyPolicy.active_keys ?? 0} of ${keyPolicy.max_active_keys} active self-service keys; ${keyPolicy.total_keys ?? 0} of ${keyPolicy.max_total_keys} lifetime creations. Shared minute limits: ${keyPolicy.rpm} requests / ${keyPolicy.tpm} tokens.`;
}
async function loadKeyPolicy() {
  if (signedOut || !csrf || keyIssuanceBusy) return;
  const version = ++keyPolicyGeneration;
  try {
    const result = await api('/account/api/key-policy');
    if (signedOut || version !== keyPolicyGeneration) return;
    keyPolicy = result; renderKeyPolicy();
  } catch (_) {
    if (!signedOut && version === keyPolicyGeneration) {
      keyPolicy = null; renderKeyPolicy();
    }
  }
}
el('create-key-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (signedOut || !csrf || keyIssuanceBusy || inaccessibleKey) return;
  if (!keyOperation) {
    const name = el('key-name').value.trim();
    if (!keyPolicy?.enabled || keyPolicy.can_create === false || !name || name.length > 100) return;
    if (!confirm('Create an API key with the displayed administrator-approved permissions and expiry? Spending and usage limits remain unchanged.')) return;
    keyOperation = {operation_id:crypto.randomUUID(), name};
  }
  const epoch = keyIssuanceEpoch;
  keyIssuanceBusy = true; ++keyPolicyGeneration; renderKeyPolicy();
  try {
    const result = await api('/account/api/keys', 'POST', keyOperation);
    if (signedOut || epoch !== keyIssuanceEpoch) return;
    if (!result.credential?.id) throw new Error('Missing key receipt');
    keyOperation = null;
    if (result.key) {
      clearSecret(); el('secret').value = result.key; el('secret-dialog').showModal();
      secretTimer = setTimeout(() => { clearSecret(); el('secret-dialog').close(); }, 120000);
      el('key-name').value = '';
      el('key-issuance-message').textContent = 'Key created. Copy it now; it will not be shown again.';
    } else {
      inaccessibleKey = result.credential.id;
      el('key-issuance-message').textContent = 'Earlier creation confirmed, but its secret cannot be recovered. Revoke the inaccessible key before creating a replacement.';
    }
    await load();
  } catch (_) {
    if (!signedOut && epoch === keyIssuanceEpoch) el('key-issuance-message').textContent = 'Creation not confirmed. Retry the same pending operation. Do not create another key in a different tab. If access or policy changed, ask an administrator to check your keys.';
  } finally { keyIssuanceBusy = false; if (!signedOut && epoch === keyIssuanceEpoch) { renderKeyPolicy(); loadKeyPolicy(); } }
});
el('recover-key').addEventListener('click', async () => {
  if (signedOut || !csrf || keyIssuanceBusy || !inaccessibleKey) return;
  if (!confirm('Revoke the key whose secret was lost? Any clients using that key will lose access. You can then create a replacement.')) return;
  const epoch = keyIssuanceEpoch;
  keyIssuanceBusy = true; renderKeyPolicy();
  try {
    await api(`/account/api/keys/${encodeURIComponent(inaccessibleKey)}/revoke`, 'POST');
    if (signedOut || epoch !== keyIssuanceEpoch) return;
    inaccessibleKey = null; el('key-name').value = '';
    el('key-issuance-message').textContent = 'Inaccessible key revoked. You may create a replacement.';
    await load();
  } catch (_) {
    if (!signedOut && epoch === keyIssuanceEpoch) el('key-issuance-message').textContent = 'Revocation not confirmed. Retry revocation before creating a replacement.';
  } finally { keyIssuanceBusy = false; if (!signedOut && epoch === keyIssuanceEpoch) { renderKeyPolicy(); loadKeyPolicy(); } }
});
window.addEventListener('pagehide', clearLocal);
load();
