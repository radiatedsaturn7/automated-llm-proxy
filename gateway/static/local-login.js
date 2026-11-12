'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let active = true, epoch = 0, busy = false, setup = false;
  let controller = null, timer = null, challenge = '', deadline = 0;
  const status = text => { $('status').textContent = text; };
  function buttons() {
    $('submit-login').disabled = busy || !active;
    $('submit-code').disabled = busy || !active;
    $('toggle-setup').disabled = busy || !active;
  }
  function clearMfa() {
    clearInterval(timer); timer = null; challenge = ''; deadline = 0;
    $('seed').textContent = ''; $('code').value = ''; $('countdown').textContent = '';
    $('seed-panel').hidden = true; $('mfa').hidden = true;
  }
  function clear() {
    ++epoch; if (controller) controller.abort(); controller = null; busy = false;
    clearMfa(); $('password').value = ''; $('grant').value = '';
    $('login').hidden = false; $('toggle-setup').hidden = false; buttons();
  }
  function countdown() {
    const seconds = Math.ceil((deadline - Date.now()) / 1000);
    if (seconds <= 0) { clear(); status('Verification expired. Start again.'); return; }
    $('countdown').textContent = `Verification expires in ${seconds} seconds.`;
  }
  function beginMfa(data, enrolling) {
    if (typeof data.challenge !== 'string' || !data.challenge || data.challenge.length > 512 ||
        !Number.isInteger(data.expires_in) || data.expires_in < 1 || data.expires_in > 300 ||
        (enrolling && (typeof data.secret !== 'string' || !/^[A-Z2-7]{16,256}$/.test(data.secret)))) {
      throw new Error('Invalid response');
    }
    clearMfa(); challenge = data.challenge; deadline = Date.now() + data.expires_in * 1000;
    $('login').hidden = true; $('toggle-setup').hidden = true; $('mfa').hidden = false;
    if (enrolling) { $('seed').textContent = data.secret; $('seed-panel').hidden = false; }
    countdown(); timer = setInterval(countdown, 250); $('code').focus();
    status(enrolling ? 'Save the secret in your authenticator, then verify a code to finish setup.' : 'Enter the code from your authenticator.');
  }
  async function send(path, payload, verification = false) {
    const ownEpoch = ++epoch; busy = true; buttons();
    controller = new AbortController(); const ownController = controller;
    const timeout = setTimeout(() => ownController.abort(), 30000);
    try {
      const response = await fetch(path, {signal: ownController.signal, method: 'POST', credentials: 'same-origin',
        cache: 'no-store', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      if (!active || ownEpoch !== epoch) return null;
      if (!response.ok) {
        if (verification && response.status === 401) {
          status('Code invalid or challenge expired. Try a fresh code; contact your administrator if setup must be restarted.');
          return null;
        }
        clear(); status(response.status === 429 ? 'Too many attempts. Try again in 15 minutes.' :
          'Sign in failed. Check credentials or contact your administrator. Restart verification if needed.');
        return null;
      }
      const data = await response.json();
      if (!active || ownEpoch !== epoch) return null;
      return {data, ownEpoch};
    } catch (_) {
      if (active && ownEpoch === epoch) { clear(); status('Sign in unavailable. Try again later.'); }
      return null;
    } finally {
      clearTimeout(timeout);
      if (ownEpoch === epoch) { controller = null; busy = false; buttons(); }
    }
  }
  function finish(result, enrolling = false) {
    if (!result || !active || result.ownEpoch !== epoch) return;
    try {
      if (!result.data || typeof result.data !== 'object') throw new Error('Invalid response');
      if (enrolling || result.data.mfa_required === true) beginMfa(result.data, enrolling);
      else if (result.data.redirect === '/admin' || result.data.redirect === '/portal') {
        const target = result.data.redirect; clear(); active = false; buttons(); location.assign(target);
      } else throw new Error('Invalid response');
    } catch (_) { clear(); status('Sign in unavailable. Try again later.'); }
  }
  $('login').onsubmit = async event => {
    event.preventDefault(); if (!active || busy) return;
    const enrolling = setup;
    const payload = {email: $('email').value, password: $('password').value};
    if (enrolling) payload.setup_grant = $('grant').value;
    // Clear DOM secrets before waiting for any network response.
    $('password').value = ''; $('grant').value = '';
    finish(await send(enrolling ? '/auth/local/mfa/enroll/start' : '/auth/local/login', payload), enrolling);
  };
  $('verify').onsubmit = async event => {
    event.preventDefault(); if (!active || busy || !challenge) return;
    if (Date.now() >= deadline) { clear(); status('Verification expired. Start again.'); return; }
    const payload = {challenge, code: $('code').value};
    $('code').value = ''; $('seed').textContent = ''; $('seed-panel').hidden = true;
    finish(await send('/auth/local/mfa/verify', payload, true));
  };
  $('toggle-setup').onclick = () => {
    if (!active || busy) return; clear(); setup = !setup; $('setup').hidden = !setup;
    $('grant').required = setup; $('submit-login').textContent = setup ? 'Begin setup' : 'Sign in';
    $('toggle-setup').textContent = setup ? 'Back to sign in' : 'Set up authenticator'; status('');
  };
  $('cancel').onclick = () => { clear(); status('Canceled locally. Any server challenge expires automatically.'); };
  window.addEventListener('pagehide', () => { active = false; clear(); });
  window.addEventListener('pageshow', () => { active = true; clear(); status(''); });
})();
