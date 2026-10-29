"use strict";

// Runs frontend/sw.js against a fake browser (self, caches, fetch) to check which
// requests it handles and what it stores. test_frontend.py runs this with `node --test`.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ORIGIN = "https://familyfoodiebook.com";
const SOURCE = fs.readFileSync(path.join(__dirname, "..", "..", "frontend", "sw.js"), "utf8");

function loadWorker() {
  const listeners = {};
  const stored = new Map(); // URL -> Response, across every cache name
  const deleted = [];
  const state = { online: true, fetched: [] };
  const key = (request) => (typeof request === "string" ? request : request.url);

  async function fetch(request) {
    state.fetched.push(key(request));
    if (!state.online) throw new TypeError("Failed to fetch");
    const response = new Response(`fresh ${key(request)}`);
    Object.defineProperty(response, "type", { value: "basic" }); // same-origin, as in a browser
    return response;
  }
  const cache = {
    async addAll(paths) {
      for (const p of paths) stored.set(new URL(p, `${ORIGIN}/sw.js`).href, new Response(`saved ${p}`));
    },
    async put(request, response) { stored.set(key(request), response); },
    async match(request) { return stored.get(key(request))?.clone(); },
  };
  const caches = {
    async open() { return cache; },
    async keys() { return ["foodie-shell-v0", "foodie-shell-v1"]; },
    async delete(name) { deleted.push(name); return true; },
  };
  const self = {
    location: new URL(`${ORIGIN}/sw.js`),
    addEventListener: (type, listener) => { listeners[type] = listener; },
    skipWaiting: async () => {},
    clients: { claim: async () => {} },
  };
  vm.runInNewContext(SOURCE, { self, caches, fetch, URL, Response });

  async function lifecycle(type) {
    let done;
    listeners[type]({ waitUntil: (promise) => { done = promise; } });
    await done;
  }
  // Resolves to the worker's response, or null when it lets the request go to the network.
  async function request(url, { method = "GET", mode = "cors" } = {}) {
    let responded = null;
    const waits = [];
    listeners.fetch({
      request: { url, method, mode },
      respondWith: (promise) => { responded = promise; },
      waitUntil: (promise) => { waits.push(promise); },
    });
    if (!responded) return null;
    const response = await responded;
    await Promise.all(waits);
    return response.text();
  }
  return { state, stored, deleted, lifecycle, request };
}

test("API and sign-in requests are never handled or stored", async () => {
  const sw = loadWorker();
  await sw.lifecycle("install");
  const before = sw.stored.size;
  assert.equal(await sw.request("https://abc123.execute-api.us-east-2.amazonaws.com/me"), null);
  assert.equal(await sw.request("https://abc123.execute-api.us-east-2.amazonaws.com/public/recipes"), null);
  assert.equal(await sw.request("https://cognito-idp.us-east-2.amazonaws.com/", { method: "POST" }), null);
  // On the local dev server the API shares the site's origin; its paths aren't app files.
  assert.equal(await sw.request(`${ORIGIN}/me`), null);
  assert.equal(await sw.request(`${ORIGIN}/recipes`, { method: "POST" }), null);
  assert.equal(sw.stored.size, before);
});

test("online, the app's files come from the network and the saved copy is refreshed", async () => {
  const sw = loadWorker();
  await sw.lifecycle("install");
  assert.equal(await sw.request(`${ORIGIN}/app.js`), `fresh ${ORIGIN}/app.js`);
  assert.equal(await sw.stored.get(`${ORIGIN}/app.js`).text(), `fresh ${ORIGIN}/app.js`);
  // A query string (e.g. a shared link's) doesn't create a second copy.
  await sw.request(`${ORIGIN}/?from=share`, { mode: "navigate" });
  assert.ok(!sw.stored.has(`${ORIGIN}/?from=share`));
});

test("offline, saved copies are used and any page falls back to the app", async () => {
  const sw = loadWorker();
  await sw.lifecycle("install");
  sw.state.online = false;
  assert.equal(await sw.request(`${ORIGIN}/app.js`), "saved app.js");
  assert.equal(await sw.request(`${ORIGIN}/config.json`), "saved config.json");
  assert.equal(await sw.request(`${ORIGIN}/`, { mode: "navigate" }), "saved ./");
  assert.equal(await sw.request(`${ORIGIN}/privacy.html`, { mode: "navigate" }), "saved privacy.html");
  assert.equal(await sw.request(`${ORIGIN}/some-old-link`, { mode: "navigate" }), "saved index.html");
  // Not an app file and not a page: left to the browser.
  assert.equal(await sw.request(`${ORIGIN}/unknown.js`), null);
});

test("offline with nothing saved, a request fails as it would without the worker", async () => {
  const sw = loadWorker();
  sw.state.online = false;
  await assert.rejects(sw.request(`${ORIGIN}/app.js`), TypeError);
});

test("a new version deletes the old version's saved copies", async () => {
  const sw = loadWorker();
  await sw.lifecycle("activate");
  assert.deepEqual(sw.deleted, ["foodie-shell-v0"]);
});
