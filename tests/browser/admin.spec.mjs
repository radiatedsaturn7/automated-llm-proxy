import { test, expect } from '@playwright/test';

test('fresh installation: real HTTP, bootstrap, administration, scoped keys and logout', async ({ page, request }) => {
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
  await expect(page.locator('#secret-dialog')).toBeVisible();
  expect(Boolean(await page.locator('#new-secret').inputValue())).toBe(true);
  await page.locator('#close-secret').click();
  const headers = { Authorization: `Bearer ${key.key}` };
  expect((await request.get('/admin/api/users', { headers })).status()).toBe(403);
  const models = await request.get('/v1/models', { headers });
  expect(models.status()).toBe(200);
  expect((await models.json()).data).toEqual([]); // No configured providers, no external model calls.

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
  // Durable credentials must not be stashed in browser storage.
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 });
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page.locator('#workspace')).toBeHidden();
  await expect(page.locator('#identity')).toHaveText('Not signed in');
  await page.reload();
  await expect(page.locator('#workspace')).toBeHidden();
  await expect(page.locator('#new-secret')).toHaveValue('');
  expect(errors).toEqual([]);
});
