import {test,expect} from '@playwright/test';
const code='12345678-1234-4234-8234-123456789abc';
const pending=()=>({request_id:code,device_name:'<img src=x onerror=alert(1)>',state:'pending',expires_at:new Date(Date.now()+600000).toISOString(),policy:{models:['chat'],expires_in_days:30,rpm:60,tpm:100000},teams:[{id:'team1',name:'Team One'}]});
async function setup(page, authenticated=true){
  await page.route('**/auth/session',r=>r.fulfill({json:{authenticated,csrf_token:authenticated?'fake-csrf':null}}));
  await page.route('**/auth/agent/enrollment/inspect',r=>r.fulfill({json:pending()}));
  await page.route('**/auth/logout',r=>r.fulfill({status:503,json:{detail:'Unavailable'}}));
  await page.goto('/relay-enroll?request_id='+code);
}
async function review(page){await page.locator('#enroll-code').fill(code);await page.getByRole('button',{name:'Review request'}).click();await expect(page.locator('#enroll-review')).toBeVisible();}
test('explicit review and approval only, no URL autoapproval, literal label and CSRF',async({page})=>{
  let approvals=[];
  await page.route('**/auth/agent/enrollment/approve',r=>{approvals.push(r.request());return r.fulfill({json:{state:'approved'}});});
  await setup(page);await expect(page.locator('#enroll-workspace')).toBeVisible();expect(approvals).toHaveLength(0);await expect(page.locator('#enroll-code')).toHaveValue('');
  await review(page);await expect(page.locator('#enroll-details')).toContainText('<img');await expect(page.locator('#enroll-details img')).toHaveCount(0);
  await expect(page.locator('#enroll-details')).toContainText('inference only');expect(approvals).toHaveLength(0);
  await page.getByRole('button',{name:'Approve this relay'}).click();await expect(page.locator('#enroll-status')).toContainText('Relay approved');
  expect(approvals).toHaveLength(1);expect(approvals[0].headers()['x-csrf-token']).toBe('fake-csrf');expect(approvals[0].postDataJSON()).toEqual({request_id:code,team_id:'team1'});
  await expect(page.locator('#enroll-details')).toBeEmpty();expect(await page.evaluate(()=>localStorage.length+sessionStorage.length)).toBe(0);
});
test('unauthenticated session cannot inspect or approve',async({page})=>{await setup(page,false);await expect(page.locator('#enroll-workspace')).toBeHidden();await expect(page.locator('#enroll-status')).toContainText('Sign in with SSO');});
test('denial is explicit and failed approval clears stale review',async({page})=>{
  await setup(page);await review(page);await page.route('**/auth/agent/enrollment/approve',r=>r.fulfill({status:409,json:{detail:'Request changed'}}));
  await page.getByRole('button',{name:'Approve this relay'}).click();await expect(page.locator('#enroll-review')).toBeHidden();await expect(page.locator('#enroll-status')).toContainText('409');
  await review(page);await page.route('**/auth/agent/enrollment/deny',r=>r.fulfill({json:{state:'denied'}}));await page.getByRole('button',{name:'Deny request'}).click();await expect(page.locator('#enroll-status')).toHaveText('Request denied.');
});
test('logout suppresses late approval response and duplicate clicks',async({page})=>{
  await setup(page);await review(page);let release, calls=0;
  await page.route('**/auth/agent/enrollment/approve',async r=>{calls++;await new Promise(resolve=>release=resolve);await r.fulfill({json:{state:'approved'}}).catch(()=>{});});
  await page.getByRole('button',{name:'Approve this relay'}).click();await expect.poll(()=>Boolean(release)).toBe(true);await expect(page.locator('#enroll-approve')).toBeDisabled();
  await page.getByRole('button',{name:'Sign out and clear'}).click();release();await page.waitForTimeout(100);await expect(page.locator('#enroll-workspace')).toBeHidden();await expect(page.locator('#enroll-status')).toContainText('Signed out locally');await expect(page.locator('#enroll-code')).toHaveValue('');expect(calls).toBe(1);
});
test('clear and pagehide discard pending review results',async({page})=>{
  await setup(page);let release;await page.route('**/auth/agent/enrollment/inspect',async r=>{await new Promise(resolve=>release=resolve);await r.fulfill({json:pending()}).catch(()=>{});});
  await page.locator('#enroll-code').fill(code);await page.getByRole('button',{name:'Review request'}).click();await expect.poll(()=>Boolean(release)).toBe(true);await page.getByRole('button',{name:'Clear this page'}).click();release();await page.waitForTimeout(100);await expect(page.locator('#enroll-review')).toBeHidden();
  await page.evaluate(()=>dispatchEvent(new Event('pagehide')));await expect(page.locator('#enroll-workspace')).toBeHidden();await expect(page.locator('#enroll-details')).toBeEmpty();
});
