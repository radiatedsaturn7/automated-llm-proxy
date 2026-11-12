import { defineConfig } from '../browser/node_modules/@playwright/test/index.mjs';
export default defineConfig({testDir:'.',testMatch:'real-oidc.spec.mjs',workers:1,retries:0,
  timeout:90000,expect:{timeout:15000},reporter:'list',
  use:{baseURL:'https://gateway.test',browserName:'chromium',trace:'off',screenshot:'off',video:'off'}});
