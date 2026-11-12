import {test,expect} from '@playwright/test';

async function openUsers(page) {
  await page.route('**/auth/session', route=>route.fulfill({json:{authenticated:true,role:'admin',email:'admin@example.test',csrf_token:'test-csrf'}}));
  await page.route('**/admin/api/usage', route=>route.fulfill({json:[]}));
  await page.route('**/admin/api/settings', route=>route.fulfill({json:{environment:'test'}}));
  await page.route('**/admin/api/users', route=>route.fulfill({json:[{id:'one',email:'member@example.test',role:'member',enabled:true}]}));
  await page.goto('/admin'); await page.locator('[data-view="users"]').click();
}

test('administrator can configure bounded usage resets without changing financial budgets', async ({page}) => {
  const writes=[];
  await page.route('**/admin/api/users/one/usage-allowance', route=>{
    if (route.request().method()==='PUT') {
      writes.push(route.request().postDataJSON());
      expect(route.request().headers()['x-csrf-token']).toBe('test-csrf');
    }
    return route.fulfill({json:{daily_request_limit:null,monthly_reset_limit:0}});
  });
  await openUsers(page);
  const prompts=[];
  page.on('dialog', dialog=>{
    prompts.push(dialog.message());
    return dialog.accept(dialog.type()==='prompt' ? prompts.length===1 ? '100' : '4' : undefined);
  });
  await page.getByRole('button',{name:'Usage allowance',exact:true}).click();
  await expect(page.locator('#status')).toContainText('Usage allowance policy saved');
  expect(writes).toEqual([{daily_request_limit:100,monthly_reset_limit:4}]);
  expect(prompts[2]).toContain('hard budgets are unchanged');
});

test('cancelling or invalid reset policy never writes configuration', async ({page}) => {
  const writes=[];
  await page.route('**/admin/api/users/one/usage-allowance', route=>{
    if (route.request().method()==='PUT') writes.push(route.request().postDataJSON());
    return route.fulfill({json:{daily_request_limit:null,monthly_reset_limit:0}});
  });
  await openUsers(page);
  page.once('dialog', dialog=>dialog.dismiss());
  await page.getByRole('button',{name:'Usage allowance',exact:true}).click();
  await expect(page.getByRole('button',{name:'Usage allowance',exact:true})).toBeEnabled();
  expect(writes).toEqual([]);
  page.once('dialog', dialog=>dialog.accept('-1'));
  await page.getByRole('button',{name:'Usage allowance',exact:true}).click();
  await expect(page.locator('#error')).toContainText('whole-number daily allowance');
  expect(writes).toEqual([]);
});
