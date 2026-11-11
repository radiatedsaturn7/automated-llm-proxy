let active = true;
let controller;
window.addEventListener('pagehide', () => {
  active = false;
  if (controller) controller.abort();
  document.querySelector('#password').value = '';
});
document.querySelector('#login').addEventListener('submit', async event => {
  event.preventDefault();
  if (!active) return;
  const button = document.querySelector('button');
  button.disabled = true;
  controller = new AbortController();
  try {
    const response = await fetch('/auth/local/login', {signal: controller.signal, method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({email: document.querySelector('#email').value, password: document.querySelector('#password').value})});
    if (!active) return;
    if (response.ok) {
      const data = await response.json();
      if (active) location.assign(data.redirect === '/admin' ? '/admin' : '/portal');
    } else document.querySelector('#status').textContent = response.status === 429 ? 'Too many attempts. Try again in 15 minutes.' : 'Sign in failed. Check credentials or contact your administrator.';
  } catch (_) {
    if (active) document.querySelector('#status').textContent = 'Sign in unavailable. Try again later.';
  } finally {
    document.querySelector('#password').value = '';
    button.disabled = false;
  }
});
