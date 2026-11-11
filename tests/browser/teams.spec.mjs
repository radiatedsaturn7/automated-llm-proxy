import {test,expect} from '@playwright/test';
async function setup(page){
  await page.route('**/auth/session',r=>r.fulfill({json:{authenticated:true,role:'member',csrf_token:'fake'}}));
  await page.route('**/teams/api',r=>r.fulfill({json:[{id:'a',name:'Team A'},{id:'b',name:'Team B'}]}));
  await page.route('**/teams/api/*/members',r=>r.fulfill({json:[{user_id:'<img src=x onerror=alert(1)>',role:'member',primary:true}]}));
  await page.route('**/teams/api/*/keys',r=>r.fulfill({json:[]}));
  await page.route('**/auth/logout',r=>r.fulfill({status:503,json:{detail:'unavailable'}}));
  await page.goto('/team-admin');
  await expect(page.locator('#team-status')).toHaveText('Team loaded.');
}
async function fillKey(page){
  await page.locator('#key-user').fill('u');await page.locator('#key-name').fill('test');
}
test('team UI renders literal metadata and clears forms and secrets on close timeout and pagehide',async({page})=>{
  await setup(page);
  await expect(page.locator('#team-members')).toContainText('<img');
  await expect(page.locator('#team-members img')).toHaveCount(0);
  await page.route('**/teams/api/a/keys',r=>r.request().method()==='POST'?r.fulfill({json:{key:'test-issued-key'}}):r.fulfill({json:[]}));
  await fillKey(page);await page.getByRole('button',{name:'Issue key',exact:true}).click();
  await expect(page.locator('#team-secret')).toHaveValue('test-issued-key');
  await page.getByRole('button',{name:'Close and clear'}).click();await expect(page.locator('#team-secret')).toHaveValue('');
  await page.clock.install();
  await page.getByRole('button',{name:'Issue key',exact:true}).click();await expect(page.locator('#team-secret')).toHaveValue('test-issued-key');
  await page.clock.fastForward(120001);await expect(page.locator('#team-secret')).toHaveValue('');
  await page.getByRole('button',{name:'Issue key',exact:true}).click();await expect(page.locator('#team-secret')).toHaveValue('test-issued-key');
  await page.evaluate(()=>dispatchEvent(new Event('pagehide')));
  await expect(page.locator('#team-secret')).toHaveValue('');await expect(page.locator('#team-workspace')).toBeHidden();
  await expect(page.locator('#key-user')).toHaveValue('');
  expect(await page.evaluate(()=>localStorage.length+sessionStorage.length)).toBe(0);
});
for(const action of ['logout','teamchange'])test(`late team mutation ignored after ${action}; concurrent writes blocked`,async({page})=>{
  await setup(page);let release;let posts=0;
  await page.route('**/teams/api/a/keys',async r=>{if(r.request().method()==='POST'){posts++;await new Promise(resolve=>release=resolve);await r.fulfill({json:{key:'late-secret'}}).catch(()=>{});}else await r.fulfill({json:[]});});
  await fillKey(page);await page.getByRole('button',{name:'Issue key',exact:true}).click();await expect.poll(()=>Boolean(release)).toBe(true);
  await page.locator('#member-id').fill('another');await page.getByRole('button',{name:'Save membership'}).click();await expect(page.locator('#team-status')).toContainText('still pending');
  if(action==='logout'){await page.getByRole('button',{name:'Sign out and clear'}).click();await expect(page.locator('#team-workspace')).toBeHidden();}
  else{await page.selectOption('#team-select','b');await expect(page.locator('#key-user')).toHaveValue('');await expect(page.locator('#team-status')).toHaveText('Team loaded.');}
  release();await page.waitForTimeout(100);await expect(page.locator('#team-secret')).toHaveValue('');await expect(page.locator('#team-secret-dialog')).not.toBeVisible();expect(posts).toBe(1);
});
test('team switch clears old rows immediately while next team loads',async({page})=>{
  await setup(page);let release;
  await page.route('**/teams/api/b/members',async r=>{await new Promise(resolve=>release=resolve);await r.fulfill({json:[]});});
  await page.selectOption('#team-select','b');await expect.poll(()=>Boolean(release)).toBe(true);await expect(page.locator('#team-members')).toBeEmpty();release();await expect(page.locator('#team-status')).toHaveText('Team loaded.');
});
