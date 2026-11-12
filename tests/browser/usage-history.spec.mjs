import {test, expect} from '@playwright/test';
const item = {id:'one',created_at:'2026-11-12T12:00:00Z',model:'<img src=x onerror=alert(1)>',input_tokens:123,output_tokens:45,cost_micros:1234,estimated:true};
const first = {since:'2026-10-13T12:00:00+00:00',until:'2026-11-12T12:00:00+00:00',records:[item],next_cursor:'opaque+/='};
async function prepare(page) {
  await page.route('**/account/api/profile', r=>r.fulfill({json:{email:'member@example.test',csrf_token:'test-csrf'}}));
  await page.route('**/account/api/keys', r=>r.fulfill({json:[]}));
  await page.route('**/auth/logout', r=>r.fulfill({status:204}));
}
test('usage history renders safe metadata, pages and refreshes fixed windows', async ({page})=>{
  await prepare(page); const urls=[];
  await page.route('**/account/api/usage?*', r=>{
    urls.push(new URL(r.request().url()));
    return r.fulfill({json:urls.at(-1).searchParams.has('cursor') ? {...first,records:[{...item,model:'second',estimated:false}],next_cursor:null} : first});
  });
  await page.goto('/portal');
  await expect(page.locator('#usage-rows')).toContainText(item.model);
  await expect(page.locator('#usage-rows img')).toHaveCount(0);
  await expect(page.locator('#usage-rows')).toContainText('0.001234');
  await expect(page.locator('#usage-rows')).toContainText('Estimated');
  await page.getByRole('button',{name:'Next page',exact:true}).click();
  await expect(page.locator('#usage-rows')).toContainText('second');
  await expect(page.locator('#usage-rows tr')).toHaveCount(1);
  expect(urls[1].searchParams.get('cursor')).toBe('opaque+/=');
  await expect(page.locator('#more-usage')).toBeHidden();
  await page.getByRole('button',{name:'Refresh usage',exact:true}).click();
  await expect(page.locator('#usage-rows')).toContainText(item.model);
  expect(urls[2].searchParams.has('cursor')).toBe(false);
  await page.getByRole('button',{name:'Sign out',exact:true}).click();
  await expect(page.locator('#usage-panel')).toBeHidden();
  await expect(page.locator('#usage-rows')).toBeEmpty();
  expect(await page.evaluate(()=>localStorage.length+sessionStorage.length)).toBe(0);
});
test('usage next-page error retains cursor for retry, no duplicate clicks', async ({page})=>{
  await prepare(page); let count=0, release;
  await page.route('**/account/api/usage?*', async r=>{
    count++;
    if (count===2) { await new Promise(resolve=>release=resolve); return r.fulfill({status:503,json:{detail:'unavailable'}}); }
    return r.fulfill({json:count===1?first:{...first,records:[],next_cursor:null}});
  });
  await page.goto('/portal'); await expect(page.locator('#more-usage')).toBeVisible();
  await page.locator('#more-usage').click(); await expect.poll(()=>Boolean(release)).toBe(true);
  await expect(page.locator('#more-usage')).toBeDisabled();
  release(); await expect(page.locator('#usage-message')).toContainText('unavailable');
  await expect(page.locator('#usage-rows tr')).toHaveCount(1);
  await page.locator('#more-usage').click();
  await expect(page.locator('#usage-message')).toContainText('No settled');
  expect(count).toBe(3);
});
for (const action of ['logout', 'pagehide']) test(`late usage response cannot restore private data after ${action}`, async ({page})=>{
  await prepare(page); let release;
  await page.route('**/account/api/usage?*', async r=>{
    await new Promise(resolve=>release=resolve); await r.fulfill({json:first});
  });
  await page.goto('/portal'); await expect.poll(()=>Boolean(release)).toBe(true);
  if (action === 'logout') await page.getByRole('button',{name:'Sign out',exact:true}).click();
  else await page.evaluate(()=>window.dispatchEvent(new Event('pagehide')));
  const response=page.waitForResponse(r=>r.url().includes('/account/api/usage?'));
  release(); await response;
  await expect(page.locator('#usage-panel')).toBeHidden();
  await expect(page.locator('#usage-rows')).toBeEmpty();
});
test('new portal load supersedes a delayed usage page', async ({page})=>{
  await prepare(page); let count=0, release;
  await page.route('**/account/api/usage?*', async r=>{
    count++;
    if (count===2) { await new Promise(resolve=>release=resolve); return r.fulfill({json:{...first,records:[{...item,model:'stale page'}],next_cursor:null}}); }
    return r.fulfill({json:count===1?first:{...first,records:[{...item,model:'fresh window'}],next_cursor:null}});
  });
  await page.goto('/portal'); await expect(page.locator('#more-usage')).toBeVisible();
  await page.locator('#more-usage').click(); await expect.poll(()=>Boolean(release)).toBe(true);
  await page.evaluate(()=>load());
  await expect(page.locator('#usage-rows')).toContainText('fresh window');
  const response=page.waitForResponse(r=>r.url().includes('cursor=')); release(); await response;
  await expect(page.locator('#usage-rows')).toContainText('fresh window');
  await expect(page.locator('#usage-rows')).not.toContainText('stale page');
});
