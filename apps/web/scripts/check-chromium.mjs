import { chromium } from "@playwright/test";

const browser = await chromium.launch({ headless: true });
const version = await browser.version();
await browser.close();

if (!version) {
  throw new Error("Chromium did not report a version");
}

console.log(`chromium=${version}`);
