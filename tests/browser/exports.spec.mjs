import { test, expect } from '@playwright/test';

async function openExports(page) {
  await page.route('**/auth/session', route=>route.fulfill({json:{authenticated:true,role:'admin',email:'export@example.test',csrf_token:'fake-csrf'}}));
  await page.route('**/admin/api/usage', route=>route.fulfill({json:[]}));
  await page.route('**/admin/api/settings', route=>route.fulfill({json:{environment:'test'}}));
  await page.goto('/admin');
  await page.locator('[data-view="exports"]').click();
}

test('bounded export paginates signed cursors and downloads JSON without storing credentials', async ({ page }) => {
  const requests=[];
  await page.route('**/admin/api/exports/audit?*', route=> {
    const query=new URL(route.request().url()).searchParams; requests.push(query);
    return route.fulfill({json:{kind:'audit',since:'2026-01-01T00:00:00Z',until:'2026-01-02T00:00:00Z',records:[{id:requests.length}],next_cursor:requests.length===1?'signed-cursor':null}});
  });
  await openExports(page);
  const download=page.waitForEvent('download');
  await page.getByRole('button',{name:'Download JSON',exact:true}).click();
  const file=await download;
  const chunks=[]; for await (const chunk of await file.createReadStream()) chunks.push(chunk);
  const data=JSON.parse(Buffer.concat(chunks).toString());
  expect(data.records).toEqual([{id:1},{id:2}]);
  expect(requests[0].has('since')).toBe(true);
  expect(requests[1].get('cursor')).toBe('signed-cursor');
  expect(requests[1].has('since')).toBe(false);
  await expect(page.locator('#export-progress')).toContainText('Downloaded 2 records');
  expect(await page.evaluate(()=>localStorage.length+sessionStorage.length)).toBe(0);
});

for (const cancel of ['button','navigation']) test(`pending export ${cancel} cancellation cannot download`, async ({ page }) => {
  let release; let downloads=0; page.on('download',()=>downloads++);
  await page.route('**/admin/api/exports/audit?*', async route=> {
    await new Promise(resolve=>{release=resolve;});
    await route.fulfill({json:{records:[{id:'late'}],next_cursor:null}}).catch(()=>{});
  });
  await openExports(page);
  await page.getByRole('button',{name:'Download JSON',exact:true}).click();
  await expect.poll(()=>Boolean(release)).toBe(true);
  if (cancel==='button') await page.getByRole('button',{name:'Cancel export',exact:true}).click();
  else await page.locator('[data-view="overview"]').click();
  release();
  await expect(page.locator('#export-progress')).toContainText('Export canceled');
  expect(downloads).toBe(0);
  await expect(page.locator('#export-start')).toBeEnabled();
});

test('expired authentication stops pagination without partial download', async ({ page }) => {
  let pages=0; let downloads=0; page.on('download',()=>downloads++);
  await page.route('**/admin/api/exports/audit?*', route=>++pages===1
    ?route.fulfill({json:{records:[{id:1}],next_cursor:'next'}})
    :route.fulfill({status:401,json:{detail:'Credential expired'}}));
  await openExports(page);
  await page.getByRole('button',{name:'Download JSON',exact:true}).click();
  await expect(page.locator('#error')).toContainText('401');
  await expect(page.locator('#export-progress')).toHaveText('No file downloaded.');
  expect(downloads).toBe(0); expect(pages).toBe(2);
});

test('oversized export page is rejected without a download', async ({ page }) => {
  let downloads=0; page.on('download',()=>downloads++);
  await page.route('**/admin/api/exports/audit?*', route=>route.fulfill({json:{records:[{data:'x'.repeat(1048577)}],next_cursor:null}}));
  await openExports(page);
  await page.getByRole('button',{name:'Download JSON',exact:true}).click();
  await expect(page.locator('#error')).toContainText('size limit');
  expect(downloads).toBe(0);
});

test('record/page cap stops without silently truncating the download', async ({ page }) => {
  let pages=0; let downloads=0; page.on('download',()=>downloads++);
  await page.route('**/admin/api/exports/audit?*', route=> {
    ++pages;
    return route.fulfill({json:{records:Array.from({length:250},(_,i)=>({id:`${pages}-${i}`})),next_cursor:`cursor-${pages}`}});
  });
  await openExports(page);
  await page.getByRole('button',{name:'Download JSON',exact:true}).click();
  await expect(page.locator('#error')).toContainText('Browser export limit reached');
  expect(pages).toBe(20); expect(downloads).toBe(0);
});

test('invalid UTC window does not send an export request', async ({ page }) => {
  let requests=0;
  await page.route('**/admin/api/exports/**', route=>{++requests;return route.fulfill({json:{records:[]}});});
  await openExports(page);
  await page.locator('#export-since').fill('2025-01-01T00:00');
  await page.locator('#export-until').fill('2025-03-01T00:00');
  await page.getByRole('button',{name:'Download JSON',exact:true}).click();
  await expect(page.locator('#error')).toContainText('at most 31 days');
  expect(requests).toBe(0);
});

test('a canceled generation cannot overwrite a later successful export', async ({ page }) => {
  let release; let calls=0; let downloads=0; page.on('download',()=>downloads++);
  await page.route('**/admin/api/exports/audit?*', async route=> {
    if (++calls===1) {
      await new Promise(resolve=>{release=resolve;});
      await route.fulfill({json:{records:[{id:'stale'}],next_cursor:null}}).catch(()=>{});
    } else await route.fulfill({json:{kind:'audit',records:[{id:'current'}],next_cursor:null}});
  });
  await openExports(page);
  await page.getByRole('button',{name:'Download JSON',exact:true}).click();
  await expect.poll(()=>Boolean(release)).toBe(true);
  await page.getByRole('button',{name:'Cancel export',exact:true}).click();
  const download=page.waitForEvent('download');
  await page.getByRole('button',{name:'Download JSON',exact:true}).click();
  await download;
  release();
  await expect(page.locator('#export-progress')).toContainText('Downloaded 1 records');
  expect(downloads).toBe(1);
});
