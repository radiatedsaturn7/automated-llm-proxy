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
