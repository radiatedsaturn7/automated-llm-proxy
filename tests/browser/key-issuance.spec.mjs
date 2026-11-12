import {test,expect} from '@playwright/test';
const policy={enabled:true,team_id:null,allowed_models:['chat'],permissions:['inference'],expires_in_days:30,rpm:60,tpm:100000,max_active_keys:3,max_total_keys:100};
async function prepare(page, enabled=true) {
  await page.route('**/account/api/profile', r=>r.fulfill({json:{csrf_token:'csrf',email:'member@test'}}));
  await page.route('**/account/api/key-policy', r=>r.fulfill({json:{...policy,enabled}}));
  await page.route('**/account/api/keys', r=>r.fulfill({json:[]}));
  await page.route('**/auth/logout', r=>r.fulfill({status:204}));
}
async function create(page) {
  await page.locator('#key-name').fill('My key');
  page.once('dialog',d=>d.accept());
  await page.locator('#create-key').click();
}
test('creation only sends name and operation, shows secret once and clears on close',async({page})=>{
  await prepare(page); let count=0;
  await page.route('**/account/api/keys',r=>{
    if(r.request().method()==='GET') return r.fulfill({json:[]});
    count++; const body=r.request().postDataJSON();
    expect(Object.keys(body).sort()).toEqual(['name','operation_id']);
    expect(r.request().headers()['x-csrf-token']).toBe('csrf');
    return r.fulfill({json:{credential:{id:'new'},key:'secret-test-value',replayed:false}});
  });
  await page.goto('/portal'); await create(page);
  await expect(page.locator('#secret')).toHaveValue('secret-test-value');
  await page.getByRole('button',{name:'Close and clear'}).click();
  await expect(page.locator('#secret')).toHaveValue(''); expect(count).toBe(1);
  expect(await page.evaluate(()=>localStorage.length+sessionStorage.length)).toBe(0);
});
test('uncertain creation keeps same operation across429 and receipt requires revoke before replacement',async({page})=>{
  await prepare(page); const attempts=[];
  await page.route('**/account/api/keys',r=>{
    if(r.request().method()==='GET') return r.fulfill({json:[]});
    attempts.push(r.request().postDataJSON());
    if(attempts.length===1) return r.abort();
    if(attempts.length===2) return r.fulfill({status:429});
    return r.fulfill({json:{credential:{id:'lost'},replayed:true,key:null}});
  });
  await page.route('**/account/api/keys/lost/revoke',r=>r.fulfill({json:{revoked:true}}));
  await page.goto('/portal'); await create(page);
  await expect(page.locator('#create-key')).toHaveText('Retry pending key creation');
  await page.locator('#create-key').click(); await expect(page.locator('#create-key')).toBeEnabled();
  await page.locator('#create-key').click();
  await expect(page.locator('#recover-key')).toBeVisible();
  expect(attempts.length).toBe(3); expect(attempts[0]).toEqual(attempts[1]); expect(attempts[1]).toEqual(attempts[2]);
  await expect(page.locator('#create-key')).toBeDisabled(); await expect(page.locator('#secret')).toHaveValue('');
  page.once('dialog',d=>d.accept()); await page.locator('#recover-key').click();
  await expect(page.locator('#create-key')).toBeEnabled(); await expect(page.locator('#recover-key')).toBeHidden();
});
for(const action of ['logout','pagehide']) test(`late creation secret stays cleared after ${action}`,async({page})=>{
  await prepare(page); let release;
  await page.route('**/account/api/keys',async r=>{
    if(r.request().method()==='GET') return r.fulfill({json:[]});
    await new Promise(resolve=>release=resolve); return r.fulfill({json:{credential:{id:'late'},key:'never-display'}});
  });
  await page.goto('/portal'); await create(page); await expect.poll(()=>Boolean(release)).toBe(true);
  await expect(page.locator('#create-key')).toBeDisabled();
  if(action==='logout') await page.locator('#logout').click(); else await page.evaluate(()=>window.dispatchEvent(new Event('pagehide')));
  const response=page.waitForResponse(r=>r.url().endsWith('/account/api/keys')&&r.request().method()==='POST');release();await response;
  await expect(page.locator('#key-issuance')).toBeHidden(); await expect(page.locator('#secret')).toHaveValue('');
});
test('default disabled policy and cancel produce no creations',async({page})=>{
  await prepare(page,false); await page.goto('/portal'); await expect(page.locator('#key-issuance')).toBeHidden();
  await page.route('**/account/api/key-policy',r=>r.fulfill({json:policy})); await page.reload();
  await page.locator('#key-name').fill('cancel'); let writes=0;
  page.on('request',r=>{if(r.method()==='POST')writes++;});
  page.once('dialog',d=>d.dismiss()); await page.locator('#create-key').click();
  expect(writes).toBe(0); await expect(page.locator('#create-key')).toHaveText('Create API key');
});
test('admin key policy requires explicit revocation warning; cancelling does not save',async({page})=>{
  await page.route('**/auth/session',r=>r.fulfill({json:{authenticated:true,role:'admin',csrf_token:'csrf'}}));
  await page.route('**/admin/api/usage',r=>r.fulfill({json:[]}));
  await page.route('**/admin/api/settings',r=>r.fulfill({json:{}}));
  await page.route('**/admin/api/users',r=>r.fulfill({json:[{id:'one',email:'member@test',enabled:true}]}));
  const writes=[];
  await page.route('**/admin/api/users/one/key-policy',r=>{
    if(r.request().method()==='PUT') { writes.push(r.request().postDataJSON());expect(r.request().headers()['x-csrf-token']).toBe('csrf'); }
    return r.fulfill({json:policy});
  });
  await page.goto('/admin');await page.locator('[data-view="users"]').click();await page.getByRole('button',{name:'Key issuance',exact:true}).click();
  await page.getByRole('button',{name:'Cancel',exact:true}).click(); expect(writes).toEqual([]);
  await page.getByRole('button',{name:'Key issuance',exact:true}).click();
  page.once('dialog',d=>{expect(d.message()).toContain('revoke ALL existing self-service keys');return d.accept();});
  await page.getByRole('button',{name:'Save key policy'}).click();
  await expect(page.locator('#status')).toContainText('Member key policy saved');expect(writes).toEqual([policy]);
});
test('late creation cannot reopen secret after concurrent profile failure',async({page})=>{
  await prepare(page);let release;
  await page.route('**/account/api/keys',async r=>{
    if(r.request().method()==='GET') return r.fulfill({json:[]});
    await new Promise(resolve=>release=resolve);return r.fulfill({json:{credential:{id:'late'},key:'never-display'}});
  });
  await page.goto('/portal');await create(page);await expect.poll(()=>Boolean(release)).toBe(true);
  await page.route('**/account/api/profile',r=>r.fulfill({status:401}));
  await page.evaluate(()=>load());await expect(page.locator('#key-issuance')).toBeHidden();
  const response=page.waitForResponse(r=>r.url().endsWith('/account/api/keys')&&r.request().method()==='POST');release();await response;
  await expect(page.locator('#secret')).toHaveValue('');await expect(page.locator('#secret-dialog')).not.toBeVisible();
});
test('exhausted creation policy displays reason and blocks new issue',async({page})=>{
  await prepare(page);
  await page.route('**/account/api/key-policy',r=>r.fulfill({json:{...policy,can_create:false,active_keys:3,total_keys:5,disabled_reason:'Active key limit reached'}}));
  await page.goto('/portal');await expect(page.locator('#create-key')).toBeDisabled();
  await expect(page.locator('#key-issuance-message')).toContainText('Active key limit reached');
  await expect(page.locator('#key-policy-summary')).toContainText('3 of 3 active');
});
test('dismissed pending admin save cannot publish late status',async({page})=>{
  await page.route('**/auth/session',r=>r.fulfill({json:{authenticated:true,role:'admin',csrf_token:'csrf'}}));
  await page.route('**/admin/api/usage',r=>r.fulfill({json:[]}));
  await page.route('**/admin/api/settings',r=>r.fulfill({json:{}}));
  await page.route('**/admin/api/users',r=>r.fulfill({json:[{id:'one',email:'member@test',enabled:true}]}));
  let release;
  await page.route('**/admin/api/users/one/key-policy',async r=>{
    if(r.request().method()==='PUT') await new Promise(resolve=>release=resolve);
    return r.fulfill({json:policy});
  });
  await page.goto('/admin');await page.locator('[data-view="users"]').click();await page.getByRole('button',{name:'Key issuance',exact:true}).click();
  page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'Save key policy'}).click();await expect.poll(()=>Boolean(release)).toBe(true);
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  const response=page.waitForResponse(r=>r.request().method()==='PUT');release();await response;
  await expect(page.locator('#status')).not.toContainText('Member key policy saved');
});
