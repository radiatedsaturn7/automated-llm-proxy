import {test,expect} from '../browser/node_modules/@playwright/test/index.mjs';
const password=process.env.OIDC_TEST_PASSWORD;
async function login(page,username){
  await page.goto('/auth/oidc/start');
  await expect(page).toHaveURL(/https:\/\/idp\.test\//);
  expect(new URL(page.url()).searchParams.get('code_challenge_method')).toBe('S256');
  expect(Boolean(new URL(page.url()).searchParams.get('nonce'))).toBe(true);
  await page.locator('#username').fill(username);await page.locator('#password').fill(password);
  await page.locator('#kc-login').click();
}
test('real Keycloak PKCE login, groups, CSRF, local and signed back-channel logout',async({page,context})=>{
  if(!password)throw new Error('Disposable OIDC fixture password required');
  await login(page,'admin-fixture');await expect(page).toHaveURL('https://gateway.test/admin');
  await expect(page.locator('#identity')).toContainText('admin-fixture@example.test');
  let session=await (await page.request.get('/auth/session')).json();expect(session.authenticated).toBe(true);expect(session.role).toBe('admin');
  const cookies=await context.cookies();const cookie=cookies.find(c=>c.name==='gateway_session');expect(cookie.secure).toBe(true);expect(cookie.httpOnly).toBe(true);
  expect((await page.request.post('/admin/api/teams',{data:{name:'No CSRF',budget_usd:1}})).status()).toBe(403);
  expect((await page.request.post('/admin/api/teams',{headers:{Origin:'https://gateway.test','X-CSRF-Token':session.csrf_token},data:{name:'Real OIDC team',budget_usd:1}})).status()).toBe(201);
  await page.getByRole('button',{name:'Sign out',exact:true}).click();expect((await (await page.request.get('/auth/session')).json()).authenticated).toBe(false);
  // IdP session remains: genuine SSO round trip recreates only a gateway session.
  await page.goto('/auth/oidc/start');await expect(page).toHaveURL('https://gateway.test/admin');
  session=await (await page.request.get('/auth/session')).json();expect(session.authenticated).toBe(true);
  // Real IdP-initiated signed logout notification, not a locally forged JWT.
  await page.goto('https://idp.test/realms/gateway-ci/protocol/openid-connect/logout?client_id=gateway&post_logout_redirect_uri=https%3A%2F%2Fgateway.test%2Fportal');
  await page.getByRole('button',{name:/logout|log out/i}).click();
  await expect(page).toHaveURL('https://gateway.test/portal');
  await expect.poll(async()=> (await (await page.request.get('/auth/session')).json()).authenticated).toBe(false);
});
test('real Keycloak authenticated user without authorized group is denied',async({page})=>{
  await login(page,'outsider-fixture');await expect(page).toHaveURL(/gateway\.test\/auth\/oidc\/callback/);
  await expect(page.locator('body')).toContainText('No unambiguous authorized SSO policy');
  expect((await (await page.request.get('/auth/session')).json()).authenticated).toBe(false);
});
