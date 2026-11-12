import { test, expect } from '@playwright/test';
import { createHmac, randomBytes } from 'node:crypto';

function totp(secret, counter) {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = '';
  for (const char of secret) {
    const value = alphabet.indexOf(char);
    if (value < 0) throw new Error('Invalid disposable authenticator encoding');
    bits += value.toString(2).padStart(5, '0');
  }
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  const message = Buffer.alloc(8); message.writeBigUInt64BE(BigInt(counter));
  const digest = createHmac('sha1', Buffer.from(bytes)).update(message).digest();
  const offset = digest[digest.length - 1] & 15;
  return String((digest.readUInt32BE(offset) & 0x7fffffff) % 1000000).padStart(6, '0');
}

test('fresh installation: real HTTP, bootstrap, administration, scoped keys and logout', async ({ page, request }) => {
  test.setTimeout(100_000); // Includes at most one 30-second TOTP counter boundary.
  const bootstrapToken = process.env.BOOTSTRAP_TOKEN;
  if (!bootstrapToken) throw new Error('Set BOOTSTRAP_TOKEN and use a fresh disposable database.');
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  for (const path of ['/health/live', '/health/ready']) {
    expect((await request.get(path)).status()).toBe(200);
  }
  expect((await request.get('/admin/api/teams')).status()).toBe(401);
  const html = await page.goto('/admin');
  expect(html.status()).toBe(200);
  expect(html.headers()['content-security-policy']).toContain("script-src 'self'");
  await expect(page.getByRole('heading', { name: 'Manage your gateway' })).toBeVisible();
  await page.getByText('First-run administrator setup', { exact: true }).click();
  await page.getByLabel('Administrator email', { exact: true }).fill('browser-admin@example.test');
  await page.getByLabel('Installation bootstrap token').fill(bootstrapToken);
  const bootstrapResponse = page.waitForResponse(r => r.url().endsWith('/bootstrap') && r.request().method() === 'POST');
  await page.getByRole('button', { name: 'Create initial administrator' }).click();
  expect((await bootstrapResponse).status()).toBe(201);
  await expect(page.locator('#secret-dialog')).toBeVisible();
  const adminKey = await page.getByLabel('Credential', { exact: true }).inputValue();
  expect(Boolean(adminKey)).toBe(true); // Avoid printing credential contents in failure reports.
  await page.locator('#close-secret').click();
  await expect(page.locator('#new-secret')).toHaveValue('');
  await page.getByLabel('Administrator API key', { exact: true }).fill(adminKey);
  await page.getByRole('button', { name: 'Connect', exact: true }).click();
  await expect(page.locator('#identity')).toContainText('browser-admin@example.test');
  await expect(page.locator('#summary')).toContainText('Runtime settings');
  await expect(page.locator('#admin-token')).toHaveValue('');

  async function create(view, fields) {
    await page.locator(`[data-view="${view}"]`).click();
    for (const [id, value] of Object.entries(fields)) await page.locator(`#field-${id}`).fill(value);
    const response = page.waitForResponse(r => r.url().endsWith(`/admin/api/${view}`) && r.request().method() === 'POST');
    await page.locator('#resource-form').getByRole('button', { name: 'Create', exact: true }).click();
    const result = await response;
    expect(result.status()).toBe(201);
    return result.json();
  }
  const team = await create('teams', { name: 'Browser integration team', budget_usd: '75' });
  await expect(page.locator('tbody')).toContainText('Browser integration team');
  const user = await create('users', { email: 'browser-member@example.test', team_id: team.id, budget_usd: '10' });
  await expect(page.locator('tbody')).toContainText('browser-member@example.test');
  const key = await create('keys', { name: 'browser-client', user_id: user.id, team_id: team.id, allowed_models: '["smoke-only"]' });
  expect(key.permissions).toEqual(["inference"]);
  await expect(page.locator('#secret-dialog')).toBeVisible();
  expect(Boolean(await page.locator('#new-secret').inputValue())).toBe(true);
  await page.locator('#close-secret').click();
  const headers = { Authorization: `Bearer ${key.key}` };
  expect((await request.get('/admin/api/users', { headers })).status()).toBe(403);
  const models = await request.get('/v1/models', { headers });
  expect(models.status()).toBe(200);
  expect((await models.json()).data).toEqual([]); // No configured providers, no external model calls.

  if (process.env.MOCK_PROVIDER_URL) {
    await create('providers', {
      name: 'Disposable TLS mock', base_url: process.env.MOCK_PROVIDER_URL,
      api_key: 'integration-only-not-a-real-key',
      models: '{"smoke-only":{"upstream_model":"mock-model","input_cost_per_million":1,"output_cost_per_million":2}}',
    });
    await expect(page.locator('#field-api_key')).toHaveValue('');
    const chat = await request.post('/v1/chat/completions', { headers, data: {
      model: 'smoke-only', messages: [{ role: 'user', content: 'Integration test' }], max_completion_tokens: 20,
    } });
    expect(chat.status()).toBe(200);
    expect((await chat.json()).choices[0].message.content).toBe('Mock upstream response');
    expect(chat.headers()['x-provider-request-id']).toBe('mock-upstream-123');
    expect(chat.headers()['x-reservation-id']).toBeTruthy();
    const stream = await request.post('/v1/chat/completions', { headers, data: {
      model: 'smoke-only', messages: [{ role: 'user', content: 'Integration stream' }], max_tokens: 20, stream: true,
    } });
    expect(stream.status()).toBe(200);
    const events = await stream.text();
    expect(events).toContain('Mock upstream response');
    expect(events).toContain('data: [DONE]');
    const embedding = await request.post('/v1/embeddings', { headers, data: { model: 'smoke-only', input: 'Integration embedding' } });
    expect(embedding.status()).toBe(200);
    expect((await embedding.json()).data[0].embedding).toEqual([0.1, 0.2, 0.3]);
    const usage = await request.get('/admin/api/usage', { headers: { Authorization: `Bearer ${adminKey}` } });
    const records = await usage.json();
    expect(records).toHaveLength(3);
    expect(records.every(row => row.estimated === false)).toBe(true);
    expect(records.reduce((sum, row) => sum + row.cost_micros, 0)).toBe(17);
    await page.locator('[data-view="keys"]').click();
  }

  const keyRow = page.locator('tbody tr').filter({ hasText: 'browser-client' });
  page.once('dialog', dialog => dialog.accept());
  await keyRow.getByRole('button', { name: 'Rotate', exact: true }).click();
  await expect(page.locator('#secret-dialog')).toBeVisible();
  const rotatedKey = await page.locator('#new-secret').inputValue();
  await page.locator('#close-secret').click();
  await expect(page.locator('tbody tr').filter({ hasText: key.id }).getByRole('button')).toHaveCount(0);
  expect((await request.get('/v1/models', { headers })).status()).toBe(401);
  expect((await request.get('/v1/models', { headers: { Authorization: `Bearer ${rotatedKey}` } })).status()).toBe(200);
  page.once('dialog', dialog => dialog.accept());
  await page.locator('tbody tr').filter({ hasText: 'browser-client' }).getByRole('button', { name: 'Revoke', exact: true }).click();
  await expect(page.locator('#status')).toHaveText('Key revoked.');
  expect((await request.get('/v1/models', { headers: { Authorization: `Bearer ${rotatedKey}` } })).status()).toBe(401);

  await page.locator('[data-view="evaluation"]').click();
  await page.getByRole('button', { name: 'Run evaluation' }).click();
  await expect(page.locator('#eval-result')).toContainText('"pass_rate": 1');
  await page.locator('[data-view="audit"]').click();
  await expect(page.locator('tbody')).toContainText('key.rotate');
  await expect(page.locator('tbody')).toContainText('team.create');
  await page.locator('[data-view="prompts"]').click();
  await page.getByLabel('Template name', { exact: true }).fill('Browser prompt registry');
  await page.getByLabel('Initial template content', { exact: true }).fill('Hello {{name}} <script>not executed</script>');
  await page.getByRole('button', { name: 'Save new template', exact: true }).click();
  await expect(page.locator('#prompt-version-content')).toContainText('Hello {{name}}');
  await expect(page.locator('#prompt-version-content script')).toHaveCount(0);
  await page.getByLabel('Variable values (JSON object)', { exact: true }).fill('{"name":"World"}');
  await page.getByRole('button', { name: 'Render preview', exact: true }).click();
  await expect(page.locator('#prompt-preview')).toContainText('Hello World <script>not executed</script>');
  await expect(page.locator('#prompt-preview script')).toHaveCount(0);
  await page.getByLabel('New version content', { exact: true }).fill('Version two {{name}}');
  await page.getByRole('button', { name: 'Append immutable version', exact: true }).click();
  await expect(page.locator('#prompt-version')).toHaveValue('2');
  await expect(page.locator('#prompt-version-content')).toHaveText('Version two {{name}}');
  await page.getByRole('button', { name: 'Evaluate and save report', exact: true }).click();
  await expect(page.locator('#prompt-reports')).toContainText('"pass_rate": 1');
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: 'Archive template', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Append immutable version', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Restore template', exact: true })).toBeVisible();
  // One existing bootstrap credential exercises the actual delegated-team UI/API.
  const teamPage = await page.context().newPage();
  teamPage.on('pageerror', error => errors.push(error.message));
  await teamPage.goto('/team-admin');
  await teamPage.locator('#team-token').fill(adminKey);
  await teamPage.getByRole('button', {name:'Connect',exact:true}).click();
  await expect(teamPage.locator('#team-workspace')).toBeVisible();
  await teamPage.selectOption('#team-select', team.id);
  await expect(teamPage.locator('#team-status')).toHaveText('Team loaded.');
  await teamPage.locator('#member-id').fill(user.id);
  await teamPage.getByRole('button', {name:'Save membership',exact:true}).click();
  await expect(teamPage.locator('#team-status')).toHaveText('Change saved.');
  await teamPage.locator('#key-user').fill(user.id);
  await teamPage.locator('#key-name').fill('Team UI inference key');
  await teamPage.getByRole('button', {name:'Issue key',exact:true}).click();
  await expect(teamPage.locator('#team-secret-dialog')).toBeVisible();
  expect(Boolean(await teamPage.locator('#team-secret').inputValue())).toBe(true);
  await teamPage.getByRole('button', {name:'Close and clear'}).click();
  await expect(teamPage.locator('#team-secret')).toHaveValue('');
  await expect(teamPage.locator('#team-keys')).toContainText('Team UI inference key');
  await teamPage.getByRole('button', {name:'Sign out and clear'}).click();
  await expect(teamPage.locator('#team-workspace')).toBeHidden();
  expect(await teamPage.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  await teamPage.close();
  // Durable credentials must not be stashed in browser storage.
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 });
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page.locator('#workspace')).toBeHidden();
  await expect(page.locator('#prompt-version-content')).toBeEmpty();
  await expect(page.locator('#prompt-content')).toHaveValue('');
  await expect(page.locator('#identity')).toHaveText('Not signed in');
  await page.reload();
  await expect(page.locator('#workspace')).toBeHidden();
  await expect(page.locator('#new-secret')).toHaveValue('');
  // Actual local MFA backend + UI, isolated from the administrator's page/context.
  const adminHeaders = { Authorization: `Bearer ${adminKey}` };
  const password = randomBytes(24).toString('base64url');
  expect((await request.put(`/admin/api/users/${user.id}/password`, {
    headers: adminHeaders, data: { password },
  })).status()).toBe(204);
  const grantResponse = await request.post(`/admin/api/users/${user.id}/mfa/setup-grant`, { headers: adminHeaders });
  expect(grantResponse.status()).toBe(200);
  const { setup_grant: grant } = await grantResponse.json();
  const memberContext = await page.context().browser().newContext({ baseURL: new URL(page.url()).origin });
  const memberPage = await memberContext.newPage();
  memberPage.on('pageerror', error => errors.push(error.message));
  let seed = '';
  try {
    await memberPage.goto('/admin/static/local-login.html');
    await memberPage.getByRole('button', { name: 'Set up authenticator', exact: true }).click();
    await memberPage.getByLabel('Email', { exact: true }).fill(user.email);
    await memberPage.getByLabel('Password', { exact: true }).fill(password);
    await memberPage.getByLabel('Setup grant', { exact: true }).fill(grant);
    await memberPage.getByRole('button', { name: 'Begin setup', exact: true }).click();
    await expect(memberPage.locator('#seed-panel')).toBeVisible();
    seed = (await memberPage.locator('#seed').textContent()).trim();
    expect(Boolean(seed)).toBe(true); // Never include seed/password/grant in assertions or artifacts.
    expect((await memberContext.request.get('/account/api/profile')).status()).toBe(401);
    const acceptedCounter = Math.floor(Date.now() / 30000);
    await memberPage.getByLabel('Authenticator code').fill(totp(seed, acceptedCounter));
    await memberPage.getByRole('button', { name: 'Verify code', exact: true }).click();
    await expect(memberPage).toHaveURL(/\/portal$/);
    await expect(memberPage.locator('#profile')).toContainText(user.email);
    expect((await memberContext.request.get('/account/api/profile')).status()).toBe(200);
    await memberPage.getByRole('button', { name: 'Sign out', exact: true }).click();
    await expect(memberPage.locator('#profile')).toBeEmpty();
    expect((await memberContext.request.get('/account/api/profile')).status()).toBe(401);
    await memberPage.goto('/admin/static/local-login.html');
    await memberPage.getByLabel('Email', { exact: true }).fill(user.email);
    await memberPage.getByLabel('Password', { exact: true }).fill(password);
    await memberPage.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(memberPage.locator('#mfa')).toBeVisible();
    await expect(memberPage.locator('#seed-panel')).toBeHidden();
    expect((await memberContext.request.get('/account/api/profile')).status()).toBe(401);
    const delay = Math.max(0, (acceptedCounter + 1) * 30000 + 100 - Date.now());
    if (delay > 30100) throw new Error('Unexpected disposable clock jump');
    if (delay) await new Promise(resolve => setTimeout(resolve, delay));
    await memberPage.getByLabel('Authenticator code').fill(totp(seed, Math.floor(Date.now() / 30000)));
    await memberPage.getByRole('button', { name: 'Verify code', exact: true }).click();
    await expect(memberPage).toHaveURL(/\/portal$/);
    await expect(memberPage.locator('#profile')).toContainText(user.email);
    expect((await memberContext.request.get('/account/api/profile')).status()).toBe(200);
    expect((await request.delete(`/admin/api/users/${user.id}/mfa`, { headers: adminHeaders })).status()).toBe(204);
    expect((await memberContext.request.get('/account/api/profile')).status()).toBe(401);
    expect(await memberPage.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  } finally {
    seed = '';
    await memberContext.close();
  }
  expect(errors).toEqual([]);
});
