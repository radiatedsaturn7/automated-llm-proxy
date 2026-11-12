import { test, expect } from '@playwright/test';

test('member portal safely renders metadata and clears credentials when logout fails', async ({ page }) => {
  await page.route('**/account/api/profile', route => route.fulfill({json:{email:'member@example.test',csrf_token:'test-csrf'}}));
  await page.route('**/account/api/keys', route => route.fulfill({json:[
    {id:'one',name:'<img src=x onerror=alert(1)>',prefix:'test',expires_at:'2099-01-01T00:00:00Z',revoked:false},
    {id:'old',name:'Expired key',prefix:'old',expires_at:'2000-01-01T00:00:00Z',revoked:false}
  ]}));
  await page.route('**/account/api/keys/one/rotate', route => {
    expect(route.request().headers()['x-csrf-token']).toBe('test-csrf');
    return route.fulfill({json:{key:'not-a-real-key'}});
  });
  await page.route('**/auth/logout', route => route.fulfill({status:503,json:{detail:'unavailable'}}));
  await page.goto('/portal');
  await expect(page.locator('#keys')).toContainText('<img src=x onerror=alert(1)>');
  await expect(page.locator('#keys img')).toHaveCount(0);
  const expired = page.locator('#keys .card').filter({hasText:'Expired key'});
  await expect(expired).toContainText('expired');
  await expect(expired.getByRole('button',{name:'Rotate',exact:true})).toHaveCount(0);
  page.once('dialog', dialog=>dialog.accept());
  await page.getByRole('button',{name:'Rotate',exact:true}).click();
  await expect(page.locator('#secret-dialog')).toBeVisible();
  await page.getByRole('button',{name:'Close and clear'}).click();
  await expect(page.locator('#secret')).toHaveValue('');
  await page.getByRole('button',{name:'Sign out',exact:true}).click();
  await expect(page.locator('#message')).toContainText('Server sign-out was not confirmed');
  await expect(page.locator('#profile')).toBeEmpty();
  await expect(page.locator('#keys')).toBeEmpty();
  expect(await page.evaluate(()=>localStorage.length+sessionStorage.length)).toBe(0);
});

test('pending portal rotation cannot display a credential after logout', async ({ page }) => {
  let finishRotation;
  await page.route('**/account/api/profile', route=>route.fulfill({json:{email:'member@example.test',csrf_token:'test-csrf'}}));
  await page.route('**/account/api/keys', route=>route.fulfill({json:[{id:'one',name:'Pending',prefix:'test',expires_at:'2099-01-01T00:00:00Z',revoked:false}]}));
  await page.route('**/account/api/keys/one/rotate', async route=> {
    await new Promise(resolve=>{finishRotation=resolve;});
    await route.fulfill({json:{key:'late-secret'}});
  });
  await page.route('**/auth/logout', route=>route.fulfill({status:204}));
  await page.goto('/portal');
  page.once('dialog', dialog=>dialog.accept());
  await page.getByRole('button',{name:'Rotate',exact:true}).click();
  await expect.poll(()=>Boolean(finishRotation)).toBe(true);
  await page.getByRole('button',{name:'Sign out',exact:true}).click();
  const response = page.waitForResponse(r=>r.url().endsWith('/one/rotate'));
  finishRotation(); await response;
  await expect(page.locator('#secret-dialog')).not.toBeVisible();
  await expect(page.locator('#secret')).toHaveValue('');
  await expect(page.locator('#keys')).toBeEmpty();
});

async function securityPage(page) {
  await page.route('**/account/api/profile', route => route.fulfill({json:{email:'member@example.test',csrf_token:'test-csrf',can_change_password:true,local_mfa_enrolled:true}}));
  await page.route('**/account/api/keys', route=>route.fulfill({json:[]}));
  await page.route('**/auth/local/options', route=>route.fulfill({json:{enabled:true}}));
  await page.goto('/portal');
  await expect(page.locator('#password-form')).toBeVisible();
}
async function passwords(page) {
  await page.getByLabel('Current password',{exact:true}).fill('old-local-passphrase-123');
  await page.getByLabel('New password (15–128 characters)',{exact:true}).fill('new-local-passphrase-456');
  await page.getByLabel('Confirm new password',{exact:true}).fill('new-local-passphrase-456');
  await page.getByLabel('Current authenticator code').fill('123456');
}

test('password change clears secrets immediately, submits once and signs out', async ({page}) => {
  let finish; const requests=[];
  await page.route('**/account/api/password', async route=>{
    requests.push(route.request()); await new Promise(resolve=>finish=resolve); await route.fulfill({status:204});
  });
  await securityPage(page); await passwords(page);
  await expect(page.getByRole('link',{name:'Sign in with a local password'})).toBeVisible();
  await page.getByRole('button',{name:'Change password and sign out'}).click();
  await expect.poll(()=>requests.length).toBe(1);
  await expect(page.locator('#change-password')).toBeDisabled();
  await expect(page.locator('#current-password')).toHaveValue('');
  await expect(page.locator('#new-password')).toHaveValue('');
  await expect(page.locator('#password-code')).toHaveValue('');
  expect(requests[0].headers()['x-csrf-token']).toBe('test-csrf');
  expect(requests[0].postDataJSON()).toEqual({current_password:'old-local-passphrase-123',new_password:'new-local-passphrase-456',code:'123456'});
  finish();
  await expect(page.locator('#message')).toContainText('Password changed');
  await expect(page.locator('#security')).toBeHidden();
  await expect(page.locator('#profile')).toBeEmpty();
  expect(await page.evaluate(()=>localStorage.length+sessionStorage.length)).toBe(0);
});

test('password mismatch, cancellation and failed response never retain passwords', async ({page}) => {
  await page.route('**/account/api/password', route=>route.fulfill({status:401,json:{detail:'<script>bad</script>'}}));
  await securityPage(page); await passwords(page);
  await page.locator('#confirm-password').fill('different-passphrase-123');
  await page.locator('#change-password').click();
  await expect(page.locator('#security-message')).toContainText('did not match');
  await expect(page.locator('#current-password')).toHaveValue('');
  await passwords(page); await page.locator('#cancel-password').click();
  await expect(page.locator('#new-password')).toHaveValue('');
  await passwords(page); await page.locator('#change-password').click();
  await expect(page.locator('#security-message')).toContainText('not confirmed');
  await expect(page.locator('#change-password')).toBeEnabled();
  await expect(page.locator('#current-password')).toHaveValue('');
  await expect(page.locator('#security-message script')).toHaveCount(0);
});

test('late password result after leaving cannot repopulate portal', async ({page}) => {
  let finish;
  await page.route('**/account/api/password', async route=>{
    await new Promise(resolve=>finish=resolve); await route.fulfill({status:204});
  });
  await securityPage(page); await passwords(page); await page.locator('#change-password').click();
  await expect.poll(()=>Boolean(finish)).toBe(true);
  await page.evaluate(()=>dispatchEvent(new Event('pagehide')));
  finish(); await page.waitForTimeout(100);
  await expect(page.locator('#message')).not.toContainText('Password changed');
  await expect(page.locator('#security')).toBeHidden();
  await expect(page.locator('#profile')).toBeEmpty();
});

test('other browser signout requires confirmation and preserves this portal', async ({page}) => {
  let count=0;
  await page.route('**/account/api/sessions/revoke-others', route=>{
    count++; expect(route.request().headers()['x-csrf-token']).toBe('test-csrf'); return route.fulfill({json:{revoked:true}});
  });
  await securityPage(page);
  page.once('dialog', dialog=>dialog.dismiss()); await page.locator('#revoke-sessions').click();
  expect(count).toBe(0);
  page.once('dialog', dialog=>dialog.accept()); await page.locator('#revoke-sessions').click();
  await expect(page.locator('#security-message')).toContainText('Other gateway browsers signed out');
  await expect(page.locator('#profile')).toContainText('member@example.test');
});

test('successful password change clears portal despite concurrent key refresh', async ({page}) => {
  let finish;
  await securityPage(page);
  await page.route('**/account/api/keys', route=>route.fulfill({json:[{id:'one',name:'Key',prefix:'test',expires_at:'2099-01-01T00:00:00Z',revoked:false}]}));
  await page.route('**/account/api/keys/one/rotate', route=>route.fulfill({json:{key:'temporary-secret'}}));
  await page.route('**/account/api/password', async route=>{
    await new Promise(resolve=>finish=resolve); await route.fulfill({status:204});
  });
  await page.reload(); await passwords(page); await page.locator('#change-password').click();
  await expect.poll(()=>Boolean(finish)).toBe(true);
  page.once('dialog', dialog=>dialog.accept());
  await page.getByRole('button',{name:'Rotate',exact:true}).click();
  await expect(page.locator('#secret-dialog')).toBeVisible();
  finish();
  await expect(page.locator('#message')).toContainText('Password changed');
  await expect(page.locator('#profile')).toBeEmpty();
  await expect(page.locator('#keys')).toBeEmpty();
  await expect(page.locator('#secret')).toHaveValue('');
  await expect(page.locator('#secret-dialog')).toBeHidden();
});
