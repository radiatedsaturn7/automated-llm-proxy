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

const seed = 'JBSWY3DPEHPK3PXP';
const mfa = {mfa_required:true,challenge:'mock-login-challenge',expires_in:300};
async function enterMfa(page) {
  await page.route('**/auth/local/login', route => route.fulfill({json:mfa}));
  await credentials(page);
  await page.getByRole('button',{name:'Sign in',exact:true}).click();
  await expect(page.getByLabel('Authenticator code')).toBeVisible();
}

test('mocked MFA requires verification, retries invalid code, and clears on cancel', async ({page}) => {
  const requests=[];
  await page.route('**/auth/local/mfa/verify', route => {
    requests.push(route.request().postDataJSON());
    return route.fulfill({status:401,json:{detail:'<script>bad</script>'}});
  });
  await enterMfa(page);
  await expect(page.locator('#password')).toHaveValue('');
  await expect(page.locator('#seed-panel')).toBeHidden();
  await page.getByLabel('Authenticator code').fill('123456');
  await page.getByRole('button',{name:'Verify code',exact:true}).click();
  await expect(page.locator('#status')).toContainText('Code invalid');
  await expect(page.getByLabel('Authenticator code')).toHaveValue('');
  await page.getByLabel('Authenticator code').fill('654321');
  await page.getByRole('button',{name:'Verify code',exact:true}).click();
  await expect.poll(()=>requests.length).toBe(2);
  expect(requests).toEqual([{challenge:mfa.challenge,code:'123456'},{challenge:mfa.challenge,code:'654321'}]);
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  await expect(page.locator('#mfa')).toBeHidden();
  await expect(page.locator('#code')).toHaveValue('');
  expect(await page.evaluate(()=>localStorage.length+sessionStorage.length)).toBe(0);
});

test('mocked enrollment displays seed only as text and expires all transient secrets', async ({page}) => {
  await page.clock.install();
  const requests=[];
  await page.route('**/auth/local/mfa/enroll/start', route => {
    requests.push(route.request().postDataJSON());
    return route.fulfill({json:{challenge:'mock-setup',secret:seed,otpauth_uri:'otpauth://totp/mock?secret='+seed,expires_in:300}});
  });
  await credentials(page);
  await page.getByRole('button',{name:'Set up authenticator',exact:true}).click();
  await page.getByLabel('Password').fill('setup password');
  await page.getByLabel('Setup grant').fill('admin-issued-test-grant');
  await page.getByRole('button',{name:'Begin setup',exact:true}).click();
  await expect(page.locator('#seed')).toHaveText(seed);
  expect(requests).toEqual([{email:'member@example.com',password:'setup password',setup_grant:'admin-issued-test-grant'}]);
  await expect(page.locator('#password')).toHaveValue('');
  await expect(page.locator('#grant')).toHaveValue('');
  await expect(page.locator('img, canvas')).toHaveCount(0);
  await page.clock.fastForward(301000);
  await expect(page.locator('#status')).toContainText('Verification expired');
  await expect(page.locator('#seed')).toHaveText('');
  await expect(page.locator('#mfa')).toBeHidden();
});

test('cancel suppresses late mocked enrollment and permits a fresh flow', async ({page}) => {
  let release;
  await page.route('**/auth/local/mfa/enroll/start', async route => {
    await new Promise(resolve=>release=resolve);
    await route.fulfill({json:{challenge:'late',secret:seed,expires_in:300}}).catch(()=>{});
  });
  await credentials(page);
  await page.getByRole('button',{name:'Set up authenticator',exact:true}).click();
  await page.getByLabel('Password').fill('setup password');
  await page.getByLabel('Setup grant').fill('setup grant');
  await page.getByRole('button',{name:'Begin setup',exact:true}).click();
  await expect.poll(()=>Boolean(release)).toBe(true);
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  release(); await page.waitForTimeout(100);
  await expect(page.locator('#seed')).toHaveText('');
  await expect(page.locator('#mfa')).toBeHidden();
  await page.getByRole('button',{name:'Back to sign in',exact:true}).click();
  await expect(page.getByRole('button',{name:'Sign in',exact:true})).toBeEnabled();
});

test('mocked verification prevents duplicates and suppresses redirect across pagehide/pageshow', async ({page}) => {
  let release; let count=0;
  await page.route('**/auth/local/mfa/verify', async route => {
    ++count; await new Promise(resolve=>release=resolve);
    await route.fulfill({json:{redirect:'/admin'}}).catch(()=>{});
  });
  await enterMfa(page);
  await page.getByLabel('Authenticator code').fill('123456');
  await page.getByRole('button',{name:'Verify code',exact:true}).click();
  await expect.poll(()=>Boolean(release)).toBe(true);
  await page.evaluate(()=>document.getElementById('verify').dispatchEvent(new Event('submit',{cancelable:true})));
  expect(count).toBe(1);
  await page.evaluate(()=>{dispatchEvent(new Event('pagehide'));dispatchEvent(new Event('pageshow'));});
  release(); await page.waitForTimeout(100);
  expect(new URL(page.url()).pathname).toBe('/admin/static/local-login.html');
  await expect(page.locator('#mfa')).toBeHidden();
  await expect(page.locator('#code')).toHaveValue('');
  await expect(page.locator('#password')).toHaveValue('');
});
