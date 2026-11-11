import {test, expect} from '@playwright/test';

async function credentials(page) {
  await page.goto('/admin/static/local-login.html');
  await page.getByLabel('Email').fill('member@example.com');
  await page.getByLabel('Password').fill('a disposable browser test password');
}

test('local login sends explicit credentials once and clears them on failure', async ({page}) => {
  const requests=[];
  await page.route('**/auth/local/login', route => {
    requests.push(route.request());
    return route.fulfill({status:401,json:{detail:'<img src=x onerror=alert(1)>'}});
  });
  await credentials(page);
  await page.getByRole('button',{name:'Sign in',exact:true}).click();
  await expect(page.locator('#status')).toContainText('Sign in failed');
  await expect(page.getByLabel('Password')).toHaveValue('');
  expect(requests).toHaveLength(1);
  expect(requests[0].postDataJSON()).toEqual({email:'member@example.com',password:'a disposable browser test password'});
  expect(requests[0].headers()['content-type']).toBe('application/json');
  await expect(page.locator('#status img')).toHaveCount(0);
  expect(await page.evaluate(()=>localStorage.length+sessionStorage.length)).toBe(0);
});

test('network failures clear password without displaying server content', async ({page}) => {
  await page.route('**/auth/local/login', route => route.abort('failed'));
  await credentials(page);
  await page.getByRole('button',{name:'Sign in',exact:true}).click();
  await expect(page.locator('#status')).toContainText('Sign in unavailable');
  await expect(page.getByLabel('Password')).toHaveValue('');
  await expect(page.getByRole('button',{name:'Sign in',exact:true})).toBeEnabled();
});

test('leaving page clears credentials and suppresses a late login redirect', async ({page}) => {
  let release;
  await page.route('**/auth/local/login', async route => {
    await new Promise(resolve=>release=resolve);
    await route.fulfill({json:{redirect:'/admin'}}).catch(()=>{});
  });
  await credentials(page);
  await page.getByRole('button',{name:'Sign in',exact:true}).click();
  await expect.poll(()=>Boolean(release)).toBe(true);
  await expect(page.getByRole('button',{name:'Sign in',exact:true})).toBeDisabled();
  await page.evaluate(()=>dispatchEvent(new Event('pagehide')));
  await expect(page.getByLabel('Password')).toHaveValue('');
  release();
  await page.waitForTimeout(100);
  expect(new URL(page.url()).pathname).toBe('/admin/static/local-login.html');
});
