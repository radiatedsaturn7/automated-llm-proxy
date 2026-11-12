import {test,expect} from '@playwright/test';
test('admin can configure Bedrock adapter with explicit limitations and cleared secret',async({page})=>{
  await page.route('**/auth/session',r=>r.fulfill({json:{authenticated:true,role:'admin',csrf_token:'test-csrf'}}));
  await page.route('**/admin/api/usage',r=>r.fulfill({json:[]}));
  await page.route('**/admin/api/settings',r=>r.fulfill({json:{}}));
  const writes=[];
  await page.route('**/admin/api/providers',r=>{
    if(r.request().method()==='POST') {
      writes.push(r.request().postDataJSON());
      expect(r.request().headers()['x-csrf-token']).toBe('test-csrf');
      return r.fulfill({status:201,json:{id:'test-provider',name:'Bedrock fixture',kind:'bedrock_converse'}});
    }
    return r.fulfill({json:[]});
  });
  await page.goto('/admin'); await page.locator('[data-view="providers"]').click();
  await page.getByLabel('Adapter',{exact:true}).selectOption('bedrock_converse');
  await expect(page.locator('#resource-form')).toContainText('IAM/SigV4 is not supported');
  await page.getByLabel('Provider name',{exact:true}).fill('Bedrock fixture');
  await page.getByLabel('Trusted provider base URL',{exact:true}).fill('https://bedrock-runtime.us-east-1.amazonaws.com');
  await page.getByLabel('Provider API key',{exact:true}).fill('synthetic-bedrock-test');
  await page.locator('#field-models').fill(JSON.stringify({chat:{upstream_model:'amazon.nova-lite-v1:0',input_cost_per_million:1,output_cost_per_million:3}}));
  await page.locator('#resource-form').getByRole('button',{name:'Create',exact:true}).click();
  await expect.poll(()=>writes.length).toBe(1);
  expect(writes[0].kind).toBe('bedrock_converse');
  expect(writes[0].api_key).toBe('synthetic-bedrock-test');
  await expect(page.locator('#field-api_key')).toHaveValue('');
  expect(await page.evaluate(()=>localStorage.length+sessionStorage.length)).toBe(0);
});
