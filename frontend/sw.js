"use strict";

// Service worker: lets the installed app (and the site) open without a connection.
// It only handles page loads and the site's own files in SHELL. Every other request,
// including all API and sign-in calls, goes straight to the network and is never
// stored, so no recipes, account details or tokens are kept here.
//
// Network first: a deploy shows up on the next load, as it did before the app could
// be installed. The saved copy is used only when the network fails.

const CACHE = "foodie-shell-v1";
const SHELL = [
  "./", "index.html", "app.js", "theme.js", "styles.css", "config.json", "manifest.json",
  "favicon.svg", "favicon.ico", "apple-touch-icon.png", "icon-192.png", "icon-512.png", "icon-maskable-512.png",
  "privacy.html", "terms.html",
];
const shellUrls = new Set(SHELL.map((path) => new URL(path, self.location).href));

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

// A new CACHE name (after changing SHELL) deletes the old copies.
self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((names) => Promise.all(names.filter((name) => name !== CACHE).map((name) => caches.delete(name))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);
  url.search = "";
  url.hash = "";
  const isPage = request.mode === "navigate";
  const saved = shellUrls.has(url.href);
  if (request.method !== "GET" || url.origin !== self.location.origin || !(isPage || saved)) return;
  event.respondWith(networkFirst(event, url.href, saved, isPage));
});

async function networkFirst(event, key, saved, isPage) {
  try {
    const response = await fetch(event.request);
    if (saved && response.ok && response.type === "basic") {
      const copy = response.clone();
      event.waitUntil(caches.open(CACHE).then((cache) => cache.put(key, copy)));
    }
    return response;
  } catch (error) {
    // Offline. Any page of the app falls back to the app itself (it routes by #hash).
    const cache = await caches.open(CACHE);
    const copy = await cache.match(key, { ignoreVary: true })
      || (isPage && await cache.match(new URL("index.html", self.location).href, { ignoreVary: true }));
    if (copy) return copy;
    throw error;
  }
}
