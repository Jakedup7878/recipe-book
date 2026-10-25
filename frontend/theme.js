"use strict";

// Seasonal theme engine. Loaded in <head> (not deferred) so the saved theme is
// applied before the page first paints. Palettes live in styles.css, keyed on
// the data-season / data-mode attributes this script sets on <html>.
// app.js uses window.RecipeTheme to change and save preferences.

(function () {
  const STORAGE_KEY = "recipe-book-appearance";
  const SEASONS = ["fall", "winter", "spring", "summer"];
  const DEFAULTS = {
    season: "auto", hemisphere: "north", followLocation: false,
    mode: "system", font: "serif", textSize: "normal", decorations: true, accents: {},
  };
  const OPPOSITE = { spring: "fall", summer: "winter", fall: "spring", winter: "summer" };
  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
  const root = document.documentElement;
  let prefs = normalize(readStored());

  function readStored() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {}; } catch { return {}; }
  }

  function writeStored(value) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(value)); } catch {}
  }

  function normalize(value) {
    const accents = {};
    for (const season of SEASONS) {
      const color = value?.accents?.[season];
      if (typeof color === "string" && /^#[0-9a-f]{6}$/i.test(color)) accents[season] = color.toLowerCase();
    }
    const merged = { ...DEFAULTS, ...value, accents };
    if (merged.hemisphere !== "south") merged.hemisphere = "north";
    merged.followLocation = merged.followLocation === true;
    return merged;
  }

  // Meteorological seasons; the Southern Hemisphere's are six months apart.
  function seasonFor(hemisphere = prefs.hemisphere, date = new Date()) {
    const month = date.getMonth();
    let season = "winter";
    if (month >= 2 && month <= 4) season = "spring";
    else if (month >= 5 && month <= 7) season = "summer";
    else if (month >= 8 && month <= 10) season = "fall";
    return hemisphere === "south" ? OPPOSITE[season] : season;
  }

  function resolvedSeason(p = prefs) {
    return p.season === "auto" ? seasonFor(p.hemisphere) : p.season;
  }

  // ---- device location (only the hemisphere is kept) ----

  // "granted", "denied" or "prompt". Checking never shows a permission prompt.
  async function locationPermission() {
    try {
      return (await navigator.permissions.query({ name: "geolocation" })).state;
    } catch {
      return "prompt";
    }
  }

  // Asks the browser for a coarse position and keeps only which side of the
  // equator it is on. Shows the permission prompt if it hasn't been answered.
  function detectHemisphere() {
    return new Promise((resolve, reject) => {
      if (!navigator.geolocation) {
        reject(new Error("This browser can't share its location."));
        return;
      }
      navigator.geolocation.getCurrentPosition(
        (position) => resolve(position.coords.latitude < 0 ? "south" : "north"),
        (error) => reject(new Error(error.code === error.PERMISSION_DENIED
          ? "Location permission was denied. You can allow it in your browser's site settings."
          : "Couldn't get this device's location. Try again, or choose a hemisphere.")),
        { enableHighAccuracy: false, maximumAge: 6 * 60 * 60 * 1000, timeout: 15000 },
      );
    });
  }

  function resolvedMode(p = prefs) {
    return p.mode === "system" ? (darkQuery.matches ? "dark" : "light") : p.mode;
  }

  // ---- color math (WCAG 2 contrast) ----

  function toRgb(hex) {
    const n = parseInt(hex.slice(1), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function toHex(rgb) {
    return "#" + rgb.map((c) => Math.round(c).toString(16).padStart(2, "0")).join("");
  }

  function luminance(hex) {
    const [r, g, b] = toRgb(hex).map((c) => {
      c /= 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  }

  function contrast(a, b) {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  }

  function mix(hex, targetHex, amount) {
    const a = toRgb(hex);
    const b = toRgb(targetHex);
    return toHex(a.map((c, i) => c + (b[i] - c) * amount));
  }

  // Nudges a custom accent lighter (dark mode) or darker (light mode) until it
  // reaches 3:1 against the page background, the WCAG minimum for UI elements.
  function readableAccent(accent, background) {
    const target = luminance(background) < 0.2 ? "#ffffff" : "#000000";
    let color = accent;
    for (let step = 1; step <= 10 && contrast(color, background) < 3; step++) {
      color = mix(accent, target, step / 10);
    }
    return color;
  }

  function textOn(color) {
    return contrast(color, "#ffffff") >= contrast(color, "#1a1410") ? "#ffffff" : "#1a1410";
  }

  // Applies a custom accent to an element that carries its own palette
  // (either <html> or a preview card with data-season / data-mode).
  function paintAccent(el, color) {
    el.style.removeProperty("--accent");
    el.style.removeProperty("--accent-text");
    if (!color) return;
    const background = getComputedStyle(el).getPropertyValue("--bg").trim();
    const accent = /^#[0-9a-f]{6}$/i.test(background) ? readableAccent(color, background) : color;
    el.style.setProperty("--accent", accent);
    el.style.setProperty("--accent-text", textOn(accent));
  }

  function apply(next = prefs) {
    prefs = normalize(next);
    const season = resolvedSeason();
    root.dataset.season = season;
    root.dataset.mode = resolvedMode();
    root.dataset.font = prefs.font;
    root.dataset.textSize = prefs.textSize;
    root.dataset.decorations = prefs.decorations ? "on" : "off";
    paintAccent(root, prefs.accents[season]);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", getComputedStyle(root).getPropertyValue("--header-from").trim());
  }

  darkQuery.addEventListener("change", () => { if (prefs.mode === "system") apply(); });

  window.RecipeTheme = {
    SEASONS,
    DEFAULTS,
    get: () => normalize(prefs),
    apply,
    save(next) { apply(next); writeStored(prefs); return normalize(prefs); },
    seasonFor,
    resolvedSeason,
    locationPermission,
    detectHemisphere,
    resolvedMode,
    paintAccent,
    contrast,
  };

  apply();
})();
