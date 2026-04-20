#!/usr/bin/env node

/**
 * Descarga un PDF desde una página protegida por login usando Playwright (Node.js).
 *
 * Requiere:
 *   npm i playwright
 *
 * Ejemplo:
 *   node download_pdf_from_active_tab.js \
 *     --cdp http://127.0.0.1:9222 \
 *     --url https://www.your-url-here \
 *     --output /Users/daandradec/regimen-simple.pdf \
 *     --timeout 120
 */

const fs = require('fs');
const fsp = require('fs/promises');
const path = require('path');
const os = require('os');
const { spawn } = require('child_process');
const http = require('http');
const https = require('https');
const { chromium } = require('playwright');

const DEFAULTS = {
  cdp: 'http://localhost:9222',
  url: 'https://www.your-url-here',
  output: 'descarga.pdf',
  timeout: 30,
  autoLaunchChrome: false,
  chromePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  chromeAppName: 'Google Chrome',
  useMainProfile: false,
  userDataDir: path.join(os.homedir(), 'Library/Application Support/Google/Chrome'),
  profileDirectory: 'Default',
  cloneMainProfile: true,
  allowUnauthFallback: false,
  match: null,
  debugJson: null,
  debugBodyLimit: 200000,
};

function parseArgs(argv) {
  const args = { ...DEFAULTS };
  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i];
    const next = argv[i + 1];
    switch (token) {
      case '--cdp': args.cdp = next; i += 1; break;
      case '--url': args.url = next; i += 1; break;
      case '--output': args.output = next; i += 1; break;
      case '--match': args.match = next; i += 1; break;
      case '--timeout': args.timeout = Number(next || DEFAULTS.timeout); i += 1; break;
      case '--auto-launch-chrome': args.autoLaunchChrome = true; break;
      case '--chrome-path': args.chromePath = next; i += 1; break;
      case '--chrome-app-name': args.chromeAppName = next; i += 1; break;
      case '--use-main-profile': args.useMainProfile = true; break;
      case '--user-data-dir': args.userDataDir = next; i += 1; break;
      case '--profile-directory': args.profileDirectory = next; i += 1; break;
      case '--clone-main-profile': args.cloneMainProfile = true; break;
      case '--allow-unauth-fallback': args.allowUnauthFallback = true; break;
      case '--debug-json': args.debugJson = next; i += 1; break;
      case '--debug-body-limit': args.debugBodyLimit = Number(next || DEFAULTS.debugBodyLimit); i += 1; break;
      default:
        break;
    }
  }
  return args;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function withTimeout(promise, ms, label) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} timeout after ${ms}ms`)), ms);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function normalizeUrl(base, maybeRelative) {
  try {
    return new URL(maybeRelative, base).toString();
  } catch {
    return maybeRelative;
  }
}

function fetchBuffer(url, headers = {}, timeoutMs = 20000) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const client = u.protocol === 'https:' ? https : http;
    const req = client.request(
      {
        hostname: u.hostname,
        port: u.port || (u.protocol === 'https:' ? 443 : 80),
        path: `${u.pathname}${u.search}`,
        method: 'GET',
        headers,
        timeout: timeoutMs,
      },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          resolve({
            ok: res.statusCode >= 200 && res.statusCode < 300,
            status: res.statusCode,
            headers: res.headers,
            body: Buffer.concat(chunks),
          });
        });
      }
    );
    req.on('timeout', () => {
      req.destroy(new Error('timeout'));
    });
    req.on('error', reject);
    req.end();
  });
}

async function cdpIsAvailable(cdpUrl, timeoutMs = 1500) {
  try {
    const res = await fetchBuffer(`${cdpUrl.replace(/\/$/, '')}/json/version`, {}, timeoutMs);
    return res.ok;
  } catch {
    return false;
  }
}

function cdpPortFromUrl(cdpUrl) {
  try {
    const u = new URL(cdpUrl);
    if (u.port) return Number(u.port);
    if (u.protocol === 'https:' || u.protocol === 'wss:') return 443;
    return 80;
  } catch {
    return 9222;
  }
}

async function createProfileClone(userDataDir, profileDirectory, port) {
  const srcRoot = userDataDir;
  const srcProfile = path.join(srcRoot, profileDirectory);
  if (!fs.existsSync(srcProfile)) return null;

  const dstRoot = path.join('/tmp', `chrome-cdp-profile-clone-${port}`);
  const dstProfile = path.join(dstRoot, profileDirectory);

  const ignoreNames = new Set([
    'Cache', 'Code Cache', 'GPUCache', 'ShaderCache', 'GrShaderCache', 'DawnCache',
    'Crashpad', 'Safe Browsing', 'BrowserMetrics',
  ]);

  const shouldIgnore = (name) => (
    ignoreNames.has(name) || name.startsWith('Singleton') || name === 'LOCK' || name === '.org.chromium.Chromium'
  );

  async function copyRecursive(src, dst) {
    const st = await fsp.stat(src);
    if (st.isDirectory()) {
      await fsp.mkdir(dst, { recursive: true });
      const entries = await fsp.readdir(src);
      for (const entry of entries) {
        if (shouldIgnore(entry)) continue;
        await copyRecursive(path.join(src, entry), path.join(dst, entry));
      }
      return;
    }
    await fsp.mkdir(path.dirname(dst), { recursive: true });
    await fsp.copyFile(src, dst);
  }

  try {
    await fsp.rm(dstRoot, { recursive: true, force: true });
    await fsp.mkdir(dstProfile, { recursive: true });

    const localState = path.join(srcRoot, 'Local State');
    if (fs.existsSync(localState)) {
      await fsp.copyFile(localState, path.join(dstRoot, 'Local State'));
    }

    await copyRecursive(srcProfile, dstProfile);
    return dstRoot;
  } catch {
    return null;
  }
}

async function tryLaunchAndWait(cmd, args, cdpUrl, waitSec = 12) {
  try {
    const p = spawn(cmd, args, {
      detached: true,
      stdio: 'ignore',
    });
    p.unref();
  } catch {
    return false;
  }

  const deadline = Date.now() + waitSec * 1000;
  while (Date.now() < deadline) {
    if (await cdpIsAvailable(cdpUrl)) return true;
    await sleep(400);
  }
  return false;
}

async function launchChromeForCdp(args) {
  const port = cdpPortFromUrl(args.cdp);
  const commands = [];

  if (args.useMainProfile) {
    commands.push({
      cmd: args.chromePath,
      args: [
        `--remote-debugging-port=${port}`,
        `--user-data-dir=${args.userDataDir}`,
        `--profile-directory=${args.profileDirectory}`,
        '--no-first-run',
        '--no-default-browser-check',
      ],
    });

    if (args.cloneMainProfile) {
      const cloned = await createProfileClone(args.userDataDir, args.profileDirectory, port);
      if (cloned) {
        console.log(`Usando clon de perfil para preservar sesión: ${cloned}/${args.profileDirectory}`);
        commands.push({
          cmd: args.chromePath,
          args: [
            `--remote-debugging-port=${port}`,
            `--user-data-dir=${cloned}`,
            `--profile-directory=${args.profileDirectory}`,
            '--no-first-run',
            '--no-default-browser-check',
          ],
        });
      }
    }
  }

  if (args.allowUnauthFallback || !args.useMainProfile) {
    commands.push(
      {
        cmd: '/usr/bin/open',
        args: ['-na', args.chromeAppName, '--args', `--remote-debugging-port=${port}`, '--no-first-run', '--no-default-browser-check'],
      },
      {
        cmd: args.chromePath,
        args: [`--remote-debugging-port=${port}`, '--no-first-run', '--no-default-browser-check'],
      },
      {
        cmd: args.chromePath,
        args: [`--remote-debugging-port=${port}`, `--user-data-dir=/tmp/chrome-cdp-profile-${port}`, '--no-first-run', '--no-default-browser-check'],
      }
    );
  }

  for (const entry of commands) {
    // eslint-disable-next-line no-await-in-loop
    if (await tryLaunchAndWait(entry.cmd, entry.args, args.cdp)) return true;
  }
  return false;
}

function looksLikePdf(buf) {
  return Buffer.isBuffer(buf) && buf.length >= 5 && buf.subarray(0, 5).toString('ascii') === '%PDF-';
}

function isPdfByHeaders(responseLike) {
  const h = responseLike.headers || {};
  const ctype = String(h['content-type'] || h['Content-Type'] || '').toLowerCase();
  const cdisp = String(h['content-disposition'] || h['Content-Disposition'] || '').toLowerCase();
  const url = String(responseLike.url || '').toLowerCase();
  return ctype.includes('application/pdf') || cdisp.includes('.pdf') || url.endsWith('.pdf') || url.includes('.pdf?');
}

function extractPdfUrlsFromText(text, baseUrl) {
  const out = [];
  const reAbs = /https?:\/\/[^"'\s>]+\.pdf(?:\?[^"'\s>]*)?/gi;
  const reAttr = /(?:src|href|file)\s*[:=]\s*["']([^"']+\.pdf(?:\?[^"']*)?)["']/gi;
  for (const m of text.match(reAbs) || []) out.push(m);
  for (const m of text.matchAll(reAttr)) out.push(normalizeUrl(baseUrl, m[1]));
  const decoded = decodeURIComponentSafe(text);
  for (const m of decoded.match(reAbs) || []) out.push(m);
  return [...new Set(out)];
}

function extractCandidateUrlsFromText(text, baseUrl) {
  const out = [];
  const keywords = ['pdf', 'visor', 'download', 'document', 'archivo', 'wp-content', 'uploads'];
  const reAbs = /https?:\/\/[^"'\s<>]+/gi;
  const reRel = /["'](\/[^"'\s<>]+)["']/g;
  const addIfInteresting = (u) => {
    const lu = u.toLowerCase();
    if (keywords.some((k) => lu.includes(k))) out.push(u);
  };
  for (const m of text.match(reAbs) || []) addIfInteresting(m);
  for (const m of text.matchAll(reRel)) addIfInteresting(normalizeUrl(baseUrl, m[1]));
  const decoded = decodeURIComponentSafe(text);
  for (const m of decoded.match(reAbs) || []) addIfInteresting(m);
  return [...new Set(out)];
}

function extractPdfUrlsFromUrl(url, baseUrl) {
  const out = [];
  try {
    const u = new URL(url, baseUrl);
    for (const key of ['file', 'pdf', 'url', 'source']) {
      const vals = u.searchParams.getAll(key);
      for (const v of vals) {
        const vv = decodeURIComponentSafe(v);
        if (vv.toLowerCase().startsWith('http') && vv.toLowerCase().includes('.pdf')) out.push(vv);
        else if (vv.toLowerCase().includes('.pdf')) out.push(normalizeUrl(baseUrl, vv));
      }
    }
  } catch {
    // noop
  }
  return [...new Set(out)];
}

function decodeURIComponentSafe(s) {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

async function getActivePage(browser) {
  const candidates = [];
  for (const context of browser.contexts()) {
    for (const page of context.pages()) {
      candidates.push(page);
      try {
        const visible = await withTimeout(
          page.evaluate(() => document.visibilityState === 'visible'),
          1200,
          'visibilityState'
        );
        if (visible) return page;
      } catch {
        // noop
      }
    }
  }
  for (const page of candidates) {
    try {
      if (!page.isClosed()) return page;
    } catch {
      // noop
    }
  }
  return null;
}

async function getLivePage(browser, current) {
  if (current) {
    try {
      if (!current.isClosed()) return current;
    } catch {
      // noop
    }
  }
  return getActivePage(browser);
}

async function navigateWithRecovery(browser, page, targetUrl, timeoutMs, trafficRecorder = null) {
  let current = page;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      await current.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
      return current;
    } catch (err) {
      const msg = String(err || '');
      const recoverable = msg.includes('Frame has been detached')
        || msg.includes('Target page, context or browser has been closed')
        || msg.includes('Execution context was destroyed');
      if (!recoverable || attempt === 3) throw err;

      let next = await getLivePage(browser, current);
      if (!next) {
        const contexts = browser.contexts();
        if (contexts.length > 0) {
          try {
            next = await contexts[0].newPage();
          } catch {
            next = null;
          }
        }
      }
      if (!next) throw err;

      if (next !== current && trafficRecorder) {
        trafficRecorder.detach(current);
        trafficRecorder.attach(next);
      }
      current = next;
      console.log(`[nav] recuperación ${attempt}/3, reintentando en página viva...`);
    }
  }
  return current;
}

async function saveBytes(buf, outputPath) {
  await fsp.mkdir(path.dirname(outputPath), { recursive: true });
  await fsp.writeFile(outputPath, buf);
}

function toB64Limited(buf, limit) {
  if (!Buffer.isBuffer(buf)) return { base64: null, truncated: false, size: 0 };
  const capped = buf.subarray(0, Math.max(0, limit));
  return {
    base64: capped.toString('base64'),
    truncated: buf.length > capped.length,
    size: buf.length,
  };
}

function sanitizeHeaders(headers) {
  const out = {};
  for (const [k, v] of Object.entries(headers || {})) {
    const lk = String(k).toLowerCase();
    if (lk === 'authorization' || lk === 'cookie' || lk === 'set-cookie') {
      out[k] = '[redacted]';
    } else {
      out[k] = String(v);
    }
  }
  return out;
}

function createTrafficRecorder(bodyLimit) {
  const entries = [];
  const requestMeta = new Map();

  const onRequest = async (req) => {
    try {
      const id = req._guid || `${Date.now()}-${Math.random()}`;
      requestMeta.set(req, {
        id,
        ts: new Date().toISOString(),
        url: req.url(),
        method: req.method(),
        resourceType: req.resourceType(),
        headers: sanitizeHeaders(await req.allHeaders().catch(() => ({}))),
        postData: req.postData() || null,
      });
    } catch {
      // noop
    }
  };

  const onResponse = async (res) => {
    try {
      const req = res.request();
      const meta = requestMeta.get(req) || {
        id: `${Date.now()}-${Math.random()}`,
        ts: new Date().toISOString(),
        url: req.url(),
        method: req.method(),
        resourceType: req.resourceType(),
        headers: {},
        postData: req.postData() || null,
      };

      const body = await res.body().catch(() => Buffer.alloc(0));
      const bodyB64 = toB64Limited(body, bodyLimit);
      entries.push({
        id: meta.id,
        request: {
          ts: meta.ts,
          url: meta.url,
          method: meta.method,
          resourceType: meta.resourceType,
          headers: meta.headers,
          postData: meta.postData,
        },
        response: {
          ts: new Date().toISOString(),
          url: res.url(),
          status: res.status(),
          ok: res.ok(),
          headers: sanitizeHeaders(await res.allHeaders().catch(() => ({}))),
          bodyBase64: bodyB64.base64,
          bodyTruncated: bodyB64.truncated,
          bodySize: bodyB64.size,
        },
      });
    } catch {
      // noop
    }
  };

  const attach = (page) => {
    page.on('request', onRequest);
    page.on('response', onResponse);
  };

  const detach = (page) => {
    try { page.off('request', onRequest); } catch { /* noop */ }
    try { page.off('response', onResponse); } catch { /* noop */ }
  };

  return {
    attach,
    detach,
    getEntries: () => entries,
  };
}

async function writeDebugJsonIfNeeded(debugPath, args, outputPath, entries) {
  if (!debugPath) return;
  const payload = {
    createdAt: new Date().toISOString(),
    note: 'Playwright captura trafico HTTP/HTTPS del navegador controlado. TCP crudo no esta disponible aqui.',
    args: {
      cdp: args.cdp,
      url: args.url,
      output: outputPath,
      timeout: args.timeout,
      match: args.match,
      profileDirectory: args.profileDirectory,
    },
    trafficCount: entries.length,
    traffic: entries,
  };
  const abs = path.resolve(debugPath);
  await fsp.mkdir(path.dirname(abs), { recursive: true });
  await fsp.writeFile(abs, JSON.stringify(payload, null, 2), 'utf8');
  console.log(`Debug HTTP/HTTPS guardado en: ${abs}`);
}

async function getIframeUrls(page) {
  const urls = [];
  for (const attr of ['src', 'data-src']) {
    const els = page.locator(`iframe[${attr}]`);
    const count = await els.count();
    for (let i = 0; i < count; i += 1) {
      const v = await els.nth(i).getAttribute(attr);
      if (v) urls.push(normalizeUrl(page.url(), v));
    }
  }
  for (const frame of page.frames()) {
    const u = frame.url();
    if (u && u !== 'about:blank') urls.push(u);
  }
  return [...new Set(urls)];
}

async function tryDirectPdfSources(page, pattern) {
  const selectors = ['iframe[src]', 'embed[src]', 'object[data]', "a[href$='.pdf']", "a[href*='.pdf?']"];
  const candidates = [];

  for (const sel of selectors) {
    const attr = sel.startsWith('iframe') || sel.startsWith('embed') ? 'src' : sel.startsWith('object') ? 'data' : 'href';
    const els = page.locator(sel);
    const count = await els.count();
    for (let i = 0; i < count; i += 1) {
      const v = await els.nth(i).getAttribute(attr);
      if (!v) continue;
      candidates.push(normalizeUrl(page.url(), v));
    }
  }

  for (const url of [...new Set(candidates)]) {
    if (pattern && !pattern.test(url)) continue;

    for (const nested of extractPdfUrlsFromUrl(url, page.url())) {
      if (pattern && !pattern.test(nested)) continue;
      try {
        // eslint-disable-next-line no-await-in-loop
        const r = await page.context().request.get(nested);
        // eslint-disable-next-line no-await-in-loop
        const b = r.ok() ? await r.body() : Buffer.alloc(0);
        if (looksLikePdf(b)) return b;
      } catch {
        // noop
      }
    }

    try {
      // eslint-disable-next-line no-await-in-loop
      const r = await page.context().request.get(url);
      if (!r.ok()) continue;
      // eslint-disable-next-line no-await-in-loop
      const b = await r.body();
      if (looksLikePdf(b)) return b;

      const ctype = String((await r.allHeaders())['content-type'] || '').toLowerCase();
      if (ctype.includes('text/html') || ctype.includes('javascript')) {
        // eslint-disable-next-line no-await-in-loop
        const t = await r.text();
        for (const ex of extractPdfUrlsFromText(t, url)) {
          if (pattern && !pattern.test(ex)) continue;
          try {
            // eslint-disable-next-line no-await-in-loop
            const er = await page.context().request.get(ex);
            if (!er.ok()) continue;
            // eslint-disable-next-line no-await-in-loop
            const eb = await er.body();
            if (looksLikePdf(eb)) return eb;
          } catch {
            // noop
          }
        }
      }
    } catch {
      // noop
    }
  }
  return null;
}

async function secondaryPdfStrategy(page, pattern, networkSamples) {
  const urlsToTry = [];
  const selectors = ['iframe[src]', 'embed[src]', 'object[data]', 'a[href]'];

  for (const sel of selectors) {
    const attr = sel.startsWith('iframe') || sel.startsWith('embed') ? 'src' : sel.startsWith('object') ? 'data' : 'href';
    const els = page.locator(sel);
    const count = await els.count();
    for (let i = 0; i < count; i += 1) {
      const v = await els.nth(i).getAttribute(attr);
      if (!v) continue;
      urlsToTry.push(normalizeUrl(page.url(), v));
    }
  }

  for (const [sampleUrl, ctype, body] of networkSamples) {
    urlsToTry.push(sampleUrl);
    urlsToTry.push(...extractPdfUrlsFromUrl(sampleUrl, page.url()));
    if (body && (ctype.includes('text') || ctype.includes('json') || ctype.includes('javascript'))) {
      const t = body.toString('utf8');
      urlsToTry.push(...extractPdfUrlsFromText(t, sampleUrl));
      urlsToTry.push(...extractCandidateUrlsFromText(t, sampleUrl));
    }
  }

  for (const candidate of [...new Set(urlsToTry)]) {
    if (pattern && !pattern.test(candidate)) continue;
    try {
      // eslint-disable-next-line no-await-in-loop
      const r = await page.context().request.get(candidate);
      if (!r.ok()) continue;
      // eslint-disable-next-line no-await-in-loop
      const b = await r.body();
      if (looksLikePdf(b)) return [candidate, b];
      const h = await r.allHeaders();
      if (String(h['content-type'] || '').toLowerCase().includes('application/pdf')) return [candidate, b];

      if (/text|json|javascript/.test(String(h['content-type'] || '').toLowerCase())) {
        // eslint-disable-next-line no-await-in-loop
        const t = await r.text();
        const nested = [...extractPdfUrlsFromText(t, candidate), ...extractCandidateUrlsFromText(t, candidate)];
        for (const n of [...new Set(nested)]) {
          if (pattern && !pattern.test(n)) continue;
          try {
            // eslint-disable-next-line no-await-in-loop
            const nr = await page.context().request.get(n);
            if (!nr.ok()) continue;
            // eslint-disable-next-line no-await-in-loop
            const nb = await nr.body();
            if (looksLikePdf(nb)) return [n, nb];
          } catch {
            // noop
          }
        }
      }
    } catch {
      // noop
    }
  }
  return null;
}

async function tertiaryIframeStrategy(page, pattern, maxUrls = 220) {
  const seeds = await getIframeUrls(page);
  for (const frame of page.frames()) {
    try {
      // eslint-disable-next-line no-await-in-loop
      const resources = await frame.evaluate(() => performance.getEntriesByType('resource').map((e) => e.name));
      if (Array.isArray(resources)) seeds.push(...resources.map(String));
    } catch {
      // noop
    }
  }

  const queue = [...new Set(seeds)];
  console.log(`Estrategia iframe profunda: ${queue.length} semillas iniciales.`);
  const visited = new Set();

  while (queue.length > 0 && visited.size < maxUrls) {
    const candidate = queue.shift();
    if (!candidate || visited.has(candidate)) continue;
    visited.add(candidate);

    if (pattern && !pattern.test(candidate)) {
      // keep exploring anyway
    }

    try {
      // eslint-disable-next-line no-await-in-loop
      const r = await page.context().request.get(candidate);
      if (!r.ok()) continue;
      // eslint-disable-next-line no-await-in-loop
      const b = await r.body();
      if (looksLikePdf(b)) return [candidate, b];
      const h = await r.allHeaders();
      const ctype = String(h['content-type'] || '').toLowerCase();
      if (ctype.includes('application/pdf')) return [candidate, b];

      if (/text|json|javascript|html/.test(ctype)) {
        // eslint-disable-next-line no-await-in-loop
        const t = await r.text();
        const discovered = [
          ...extractPdfUrlsFromText(t, candidate),
          ...extractCandidateUrlsFromText(t, candidate),
          ...extractPdfUrlsFromUrl(candidate, candidate),
        ];
        for (const m of t.matchAll(/(?:src|href)\s*=\s*["']([^"']+)["']/gi)) {
          discovered.push(normalizeUrl(candidate, m[1]));
        }
        for (const n of [...new Set(discovered)]) {
          if (!visited.has(n)) queue.push(n);
        }
      }
    } catch {
      // noop
    }
  }

  console.log(`Estrategia iframe profunda: exploradas ${visited.size} URLs sin PDF.`);
  return null;
}

async function requestBytesWithCookieHeader(page, targetUrl, referer) {
  try {
    const cookies = await page.context().cookies([targetUrl]);
    const cookieHeader = cookies
      .filter((c) => c.name && c.value)
      .map((c) => `${c.name}=${c.value}`)
      .join('; ');
    const userAgent = await page.evaluate(() => navigator.userAgent).catch(() => 'Mozilla/5.0');

    const headers = { 'User-Agent': String(userAgent), Referer: referer };
    if (cookieHeader) headers.Cookie = cookieHeader;

    const res = await fetchBuffer(targetUrl, headers, 20000);
    if (!res.ok) return null;
    return res.body;
  } catch {
    return null;
  }
}

async function authenticatedIframeFetchStrategy(page, pattern) {
  const iframeUrls = await getIframeUrls(page);
  console.log(`Estrategia cookies+iframe: ${iframeUrls.length} iframes/frames detectados.`);
  const candidates = [];

  for (const iframeUrl of iframeUrls) {
    candidates.push(iframeUrl);
    candidates.push(...extractPdfUrlsFromUrl(iframeUrl, page.url()));
    try {
      // eslint-disable-next-line no-await-in-loop
      const r = await page.context().request.get(iframeUrl);
      if (!r.ok()) continue;
      // eslint-disable-next-line no-await-in-loop
      const b = await r.body();
      if (looksLikePdf(b)) return [iframeUrl, b];
      const ctype = String((await r.allHeaders())['content-type'] || '').toLowerCase();
      if (/text|json|javascript|html/.test(ctype)) {
        // eslint-disable-next-line no-await-in-loop
        const t = await r.text();
        candidates.push(...extractPdfUrlsFromText(t, iframeUrl));
        candidates.push(...extractCandidateUrlsFromText(t, iframeUrl));
      }
    } catch {
      // noop
    }
  }

  for (const candidate of [...new Set(candidates)]) {
    if (pattern && !pattern.test(candidate)) continue;
    try {
      // eslint-disable-next-line no-await-in-loop
      const r = await page.context().request.get(candidate);
      if (r.ok()) {
        // eslint-disable-next-line no-await-in-loop
        const b = await r.body();
        if (looksLikePdf(b)) return [candidate, b];
        if (String((await r.allHeaders())['content-type'] || '').toLowerCase().includes('application/pdf')) return [candidate, b];
      }
    } catch {
      // noop
    }

    // fallback explicito HTTP con cookies
    // eslint-disable-next-line no-await-in-loop
    const b2 = await requestBytesWithCookieHeader(page, candidate, page.url());
    if (looksLikePdf(b2)) return [candidate, b2];
  }

  return null;
}

async function expectDownloadStrategy(page, outputPath, timeoutSec = 20) {
  const timeoutMs = Math.max(1, timeoutSec) * 1000;
  const selectors = [
    'a[download]',
    "a[href*='.pdf']",
    "a[href*='download']",
    "button:has-text('Descargar')",
    "button:has-text('Download')",
  ];

  for (const sel of selectors) {
    const loc = page.locator(sel);
    if ((await loc.count()) < 1) continue;
    try {
      const [dl] = await Promise.all([
        page.waitForEvent('download', { timeout: timeoutMs }),
        loc.first().click(),
      ]);
      await dl.saveAs(outputPath);
      return true;
    } catch {
      // noop
    }
  }

  for (const frame of page.frames()) {
    for (const sel of selectors) {
      try {
        const loc = frame.locator(sel);
        if ((await loc.count()) < 1) continue;
        const [dl] = await Promise.all([
          page.waitForEvent('download', { timeout: timeoutMs }),
          loc.first().click(),
        ]);
        await dl.saveAs(outputPath);
        return true;
      } catch {
        // noop
      }
    }
  }

  const iframeUrls = await getIframeUrls(page);
  for (const iframeUrl of iframeUrls) {
    let tempPage = null;
    try {
      tempPage = await page.context().newPage();
      const [dl] = await Promise.all([
        tempPage.waitForEvent('download', { timeout: timeoutMs }),
        tempPage.goto(iframeUrl, { waitUntil: 'domcontentloaded' }),
      ]);
      await dl.saveAs(outputPath);
      return true;
    } catch {
      // noop
    } finally {
      if (tempPage) {
        await tempPage.close().catch(() => {});
      }
    }
  }

  return false;
}

async function printToPdfFallback(page, outputPath) {
  try {
    await page.emulateMedia({ media: 'screen' });
  } catch {
    // noop
  }

  try {
    await page.pdf({ path: outputPath, printBackground: true, preferCSSPageSize: true });
    return true;
  } catch {
    // noop
  }

  const iframeUrls = await getIframeUrls(page);
  for (const iframeUrl of iframeUrls) {
    let tempPage = null;
    try {
      tempPage = await page.context().newPage();
      await tempPage.goto(iframeUrl, { waitUntil: 'domcontentloaded', timeout: 20000 });
      await tempPage.pdf({ path: outputPath, printBackground: true, preferCSSPageSize: true });
      return true;
    } catch {
      // noop
    } finally {
      if (tempPage) {
        await tempPage.close().catch(() => {});
      }
    }
  }

  return false;
}

async function main() {
  const args = parseArgs(process.argv);
  const outputPath = path.resolve(args.output);
  const pattern = args.match ? new RegExp(args.match) : null;
  console.log(`[init] cdp=${args.cdp} url=${args.url} timeout=${args.timeout}s`);

  if (!(await cdpIsAvailable(args.cdp))) {
    if (args.autoLaunchChrome) {
      console.log('[cdp] no activo, intentando iniciarlo...');
      const launched = await launchChromeForCdp(args);
      if (!launched) {
        console.error('[cdp] no fue posible activarlo con tu configuración actual.');
      }
    } else {
      console.error('[cdp] no disponible y --auto-launch-chrome no fue especificado.');
    }
  }

  let browser;
  try {
    console.log('[cdp] conectando a browser...');
    browser = await withTimeout(chromium.connectOverCDP(args.cdp), 15000, 'connectOverCDP');
    console.log('[cdp] conectado');
  } catch (err) {
    console.error('No se pudo conectar a CDP.');
    console.error(`Detalle: ${String(err)}`);
    return 1;
  }

  console.log('[cdp] buscando pestaña activa...');
  let page = await getActivePage(browser);
  console.log('[cdp] búsqueda de pestaña finalizada');
  if (!page) {
    console.error('No se encontró ninguna pestaña en la sesión de Chrome conectada.');
    await browser.close().catch(() => {});
    return 1;
  }

  const trafficRecorder = createTrafficRecorder(args.debugBodyLimit);
  trafficRecorder.attach(page);

  console.log(`Pestaña inicial: ${page.url()}`);
  page.setDefaultNavigationTimeout(Math.max(15000, args.timeout * 1000));
  page.setDefaultTimeout(Math.max(15000, args.timeout * 1000));
  if (args.url) {
    console.log(`Navegando a: ${args.url}`);
    page = await navigateWithRecovery(
      browser,
      page,
      args.url,
      Math.max(15000, args.timeout * 1000),
      trafficRecorder
    );
    console.log(`URL cargada: ${page.url()}`);
  }

  const directData = await tryDirectPdfSources(page, pattern);
  if (directData) {
    await saveBytes(directData, outputPath);
    console.log(`PDF guardado en: ${outputPath}`);
    await browser.close().catch(() => {});
    return 0;
  }

  const captured = new Map();
  const networkSamples = [];

  const onResponse = async (resp) => {
    try {
      const body = await resp.body();
      if (!body || !resp.ok()) return;
      const headers = await resp.allHeaders();
      const contentType = String(headers['content-type'] || '').toLowerCase();
      const lowerUrl = resp.url().toLowerCase();
      const interesting = contentType.includes('text/')
        || contentType.includes('json')
        || contentType.includes('javascript')
        || contentType.includes('pdf')
        || contentType.includes('octet-stream')
        || ['visor', 'regimen', 'pdf', 'download', 'file', 'document'].some((k) => lowerUrl.includes(k));
      if (interesting && networkSamples.length < 250) {
        networkSamples.push([resp.url(), contentType, body.subarray(0, 600000)]);
      }

      if ((!pattern || pattern.test(resp.url())) && (isPdfByHeaders({ headers, url: resp.url() }) || looksLikePdf(body))) {
        captured.set(resp.url(), body);
      }
    } catch {
      // noop
    }
  };

  page.on('response', onResponse);
  try {
    await page.reload({ waitUntil: 'domcontentloaded', timeout: 15000 });
  } catch {
    // noop
  }

  const deadline = Date.now() + args.timeout * 1000;
  while (Date.now() < deadline && captured.size === 0) {
    const livePage = await getLivePage(browser, page);
    if (!livePage) {
      console.log('La pestaña se cerró y no hay otra pestaña activa para continuar.');
      break;
    }
    if (livePage !== page) {
      trafficRecorder.detach(page);
      page.off('response', onResponse);
      page = livePage;
      trafficRecorder.attach(page);
      page.on('response', onResponse);
      console.log(`Cambiando a pestaña activa: ${page.url()}`);
    }
    try {
      await page.waitForTimeout(300);
    } catch {
      break;
    }
  }

  page.off('response', onResponse);

  if (captured.size > 0) {
    const [url, pdfBytes] = captured.entries().next().value;
    await saveBytes(pdfBytes, outputPath);
    console.log(`PDF capturado desde: ${url}`);
    console.log(`PDF guardado en: ${outputPath}`);
    trafficRecorder.detach(page);
    await browser.close().catch(() => {});
    return 0;
  }

  console.log('No hubo PDF directo. Ejecutando estrategia secundaria...');
  const fallback1 = await secondaryPdfStrategy(page, pattern, networkSamples);
  if (fallback1) {
    const [u, b] = fallback1;
    await saveBytes(b, outputPath);
    console.log(`PDF encontrado con estrategia secundaria: ${u}`);
    console.log(`PDF guardado en: ${outputPath}`);
    trafficRecorder.detach(page);
    await browser.close().catch(() => {});
    return 0;
  }

  console.log('Estrategia secundaria sin éxito. Ejecutando estrategia iframe profunda...');
  const fallback2 = await tertiaryIframeStrategy(page, pattern);
  if (fallback2) {
    const [u, b] = fallback2;
    await saveBytes(b, outputPath);
    console.log(`PDF encontrado en estrategia iframe profunda: ${u}`);
    console.log(`PDF guardado en: ${outputPath}`);
    trafficRecorder.detach(page);
    await browser.close().catch(() => {});
    return 0;
  }

  console.log('Estrategia iframe profunda sin éxito. Ejecutando cookies+iframe request...');
  const fallback3 = await authenticatedIframeFetchStrategy(page, pattern);
  if (fallback3) {
    const [u, b] = fallback3;
    await saveBytes(b, outputPath);
    console.log(`PDF encontrado por cookies+iframe: ${u}`);
    console.log(`PDF guardado en: ${outputPath}`);
    trafficRecorder.detach(page);
    await browser.close().catch(() => {});
    return 0;
  }

  console.log('Cookies+iframe sin éxito. Ejecutando expect_download/content-disposition...');
  if (await expectDownloadStrategy(page, outputPath, Math.max(20, Math.floor(args.timeout / 2)))) {
    console.log(`Archivo descargado por expect_download en: ${outputPath}`);
    trafficRecorder.detach(page);
    await browser.close().catch(() => {});
    return 0;
  }

  console.log('expect_download sin éxito. Ejecutando print-to-PDF como último recurso...');
  if (await printToPdfFallback(page, outputPath)) {
    console.log(`PDF generado por impresión de página en: ${outputPath}`);
    trafficRecorder.detach(page);
    await browser.close().catch(() => {});
    return 0;
  }

  console.error('No se capturó ningún PDF dentro del tiempo límite. También fallaron cookies+iframe, expect_download y print-to-PDF.');
  await writeDebugJsonIfNeeded(args.debugJson, args, outputPath, trafficRecorder.getEntries());
  trafficRecorder.detach(page);
  await browser.close().catch(() => {});
  return 2;
}

main().then((code) => process.exit(code)).catch((err) => {
  console.error(String(err));
  process.exit(1);
});
