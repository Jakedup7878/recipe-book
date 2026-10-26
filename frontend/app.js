"use strict";

// Recipe Book single-page app. Its own sign-in and sign-up pages talk to the
// Cognito user pool API directly (on the local dev server, to a fake Cognito),
// then the Python API is called with the ID token. All user text is inserted
// with textContent (see h()) to block XSS. Seasonal theming lives in theme.js.

let config;
const view = document.getElementById("view");
const nav = document.getElementById("nav");
const footer = document.getElementById("footer-links");
const notice = document.getElementById("notice");
const Theme = window.RecipeTheme;

// ---------- small DOM helper ----------

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === false || value == null) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "text") el.textContent = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// Skips null/false like h() does: replaceChildren would print them as "null"/"false".
function render(...nodes) {
  view.replaceChildren(...nodes.flat().filter((node) => node != null && node !== false));
}

function showNotice(message) {
  notice.textContent = message;
  notice.hidden = !message;
}

// A message to show on the next page, e.g. "Password changed" after a redirect.
let flashMessage = "";

function go(hash, message = "") {
  flashMessage = message;
  if (location.hash === hash) route();
  else location.hash = hash;
}

function formatDate(iso) {
  return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function capitalize(word) {
  return word.charAt(0).toUpperCase() + word.slice(1);
}

// ---------- auth (Cognito user pool API) ----------

// sessionStorage clears when the tab closes; "Keep me signed in" uses localStorage instead.
const session = {
  get(key) { try { return JSON.parse(sessionStorage.getItem(key)); } catch { return null; } },
  set(key, value) { try { sessionStorage.setItem(key, JSON.stringify(value)); } catch {} },
  remove(key) { try { sessionStorage.removeItem(key); } catch {} },
};

const tokenStore = {
  read() {
    for (const storage of [sessionStorage, localStorage]) {
      try {
        const tokens = JSON.parse(storage.getItem("tokens"));
        if (tokens) return tokens;
      } catch {}
    }
    return null;
  },
  write(tokens) {
    this.clear();
    try { (tokens.remember ? localStorage : sessionStorage).setItem("tokens", JSON.stringify(tokens)); } catch {}
  },
  clear() {
    for (const storage of [sessionStorage, localStorage]) {
      try { storage.removeItem("tokens"); } catch {}
    }
  },
};

// Keep in sync with the user pool's password policy in infra/recipe_stack.py.
const PASSWORD_RULES = [
  [/.{10,}/, "At least 10 characters"],
  [/[a-z]/, "A lowercase letter"],
  [/[A-Z]/, "An uppercase letter"],
  [/[0-9]/, "A number"],
];

const AUTH_MESSAGES = {
  NotAuthorizedException: "Incorrect email or password.",
  UserNotFoundException: "Incorrect email or password.",
  UsernameExistsException: "An account with this email already exists. Try signing in instead.",
  InvalidPasswordException: "That password doesn't meet the requirements.",
  CodeMismatchException: "That code isn't right. Check the email and try again.",
  ExpiredCodeException: "That code has expired. Send yourself a new one.",
  LimitExceededException: "Too many attempts. Please wait a few minutes and try again.",
  TooManyRequestsException: "Too many attempts. Please wait a few minutes and try again.",
  TooManyFailedAttemptsException: "Too many attempts. Please wait a few minutes and try again.",
  UserNotConfirmedException: "Please confirm your email address first.",
};

class AuthError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

async function cognito(action, body) {
  let response;
  try {
    response = await fetch(config.cognitoEndpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": `AWSCognitoIdentityProviderService.${action}`,
      },
      body: JSON.stringify({ ClientId: config.clientId, ...body }),
    });
  } catch {
    throw new AuthError("Network", "Couldn't reach the sign-in service. Check your connection and try again.");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const code = (data.__type || "").split("#").pop();
    const raw = data.message || data.Message || "";
    const message = code === "NotAuthorizedException" && /attempts exceeded/i.test(raw)
      ? AUTH_MESSAGES.LimitExceededException
      : AUTH_MESSAGES[code] || raw || "Something went wrong. Please try again.";
    throw new AuthError(code, message);
  }
  return data;
}

function saveSession(result, remember, previous = {}) {
  tokenStore.write({
    idToken: result.IdToken,
    accessToken: result.AccessToken,
    refreshToken: result.RefreshToken || previous.refreshToken,  // refreshes don't return a new one
    expiresAt: Date.now() + (result.ExpiresIn - 60) * 1000,
    remember,
  });
}

async function signIn(email, password, remember) {
  const data = await cognito("InitiateAuth", {
    AuthFlow: "USER_PASSWORD_AUTH",
    AuthParameters: { USERNAME: email.trim(), PASSWORD: password },
  });
  if (!data.AuthenticationResult) {
    throw new AuthError(data.ChallengeName, "This account needs an extra sign-in step that isn't supported yet.");
  }
  saveSession(data.AuthenticationResult, remember);
  await loadMe();
  // Apply the name chosen at sign-up, in case the token didn't carry it.
  const pending = session.get("pendingSignup");
  if (pending?.name && me && pending.email === email.trim() && me.displayName !== pending.name) {
    me = await api("/me", { method: "PUT", body: { displayName: pending.name } });
  }
  session.remove("pendingSignup");
}

async function idToken() {
  const tokens = tokenStore.read();
  if (!tokens) return null;
  if (Date.now() < tokens.expiresAt) return tokens.idToken;
  if (!tokens.refreshToken) return null;
  try {
    const data = await cognito("InitiateAuth", {
      AuthFlow: "REFRESH_TOKEN_AUTH",
      AuthParameters: { REFRESH_TOKEN: tokens.refreshToken },
    });
    saveSession(data.AuthenticationResult, tokens.remember, tokens);
    return tokenStore.read().idToken;
  } catch {
    tokenStore.clear();
    return null;
  }
}

async function signOut() {
  const tokens = tokenStore.read();
  tokenStore.clear();
  me = null;
  go("#/", "You're signed out.");
  // Revoke the refresh token in the background so a copied token can't be used later.
  if (tokens?.refreshToken) cognito("RevokeToken", { Token: tokens.refreshToken }).catch(() => {});
}

// Pages that need an account send people to sign in, then back here.
function requireSignIn(message = "Please sign in to continue.") {
  session.set("returnTo", location.hash || "#/");
  go("#/signin", message);
}

// ---------- API ----------

// Bumped by every route(). A page's data loads (GETs) that finish after you've
// moved on to another page are dropped, so a slow page can't draw over a newer one.
let routeSeq = 0;
class StaleRoute extends Error {}

async function api(path, { method = "GET", body } = {}) {
  const seq = routeSeq;
  const token = await idToken();
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(config.apiUrl + path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (method === "GET" && seq !== routeSeq) throw new StaleRoute(`Dropped ${path}: the page changed.`);
  if (response.status === 204) return null;
  const data = await response.json().catch(() => ({}));
  if (response.status === 429) {
    const wait = response.headers.get("Retry-After");
    throw new Error(`Too many requests. Please wait${wait ? ` ${wait} seconds` : ""} and try again.`);
  }
  if (response.status === 401) {
    tokenStore.clear();
    me = null;
    throw new Error("Your session has ended. Please sign in again.");
  }
  if (!response.ok) throw new Error(data.message || `Request failed (${response.status}).`);
  return data;
}

let me = null;

async function loadMe() {
  me = (await idToken()) ? await api("/me") : null;
  // The account's saved look wins over this device's, so it follows you between devices.
  if (me?.preferences) Theme.save(me.preferences);
}

// ---------- views ----------

// The top bar is Home, Family and an account menu holding profile, appearance
// and sign out. Signed out, it's Home plus the sign-in links; appearance is in
// the footer so the look can still be changed without an account.
function renderNav() {
  const page = (location.hash || "#/").split("/")[1] || "";
  const link = (href, label, key) =>
    h("a", { href, text: label, "aria-current": page === key ? "page" : null });
  const links = me
    ? [link("#/", "Home", ""), link("#/family", "Family", "family"), accountMenu(page)]
    : [
        link("#/", "Home", ""),
        link("#/signin", "Sign in", "signin"),
        h("a", { class: "button", href: "#/signup", text: "Create account" }),
      ];
  nav.replaceChildren(...links);
  footer.replaceChildren(...(me ? [] : [link("#/appearance", "Appearance", "appearance")]));
}

// A disclosure (button + list of links) rather than an ARIA menu: it works
// with Tab and Enter like the rest of the nav, and Escape closes it.
function accountMenu(page) {
  const menu = h("div", { class: "menu", id: "account-menu", hidden: true },
    h("div", { class: "menu-head" },
      h("strong", { text: me.displayName }),
      h("div", { class: "muted", text: me.email })),
    h("a", { href: "#/profile", text: "Your profile", "aria-current": page === "profile" ? "page" : null }),
    h("a", { href: "#/appearance", text: "Appearance", "aria-current": page === "appearance" ? "page" : null }),
    h("hr"),
    h("button", { type: "button", class: "menu-item", text: "Sign out", onclick: signOut }));
  const toggle = h("button", {
    type: "button",
    class: "account-button",
    "aria-expanded": "false",
    "aria-controls": "account-menu",
    "aria-label": `Account menu for ${me.displayName}`,
    "data-current": ["profile", "appearance"].includes(page) ? "true" : null,
    onclick: () => setMenuOpen(menu.hidden),
  }, avatar(me.displayName), h("span", { class: "account-name", text: me.displayName }),
    h("span", { class: "caret", "aria-hidden": "true", text: "▾" }));
  const wrap = h("div", { class: "account" }, toggle, menu);

  function setMenuOpen(open, { focusToggle = false } = {}) {
    menu.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (focusToggle) toggle.focus();
  }
  wrap.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) setMenuOpen(false, { focusToggle: true });
  });
  wrap.addEventListener("focusout", (event) => {
    if (!wrap.contains(event.relatedTarget)) setMenuOpen(false);
  });
  closeAccountMenu = () => setMenuOpen(false);
  return wrap;
}

let closeAccountMenu = () => {};
document.addEventListener("click", (event) => {
  if (!event.target.closest?.(".account")) closeAccountMenu();
});

// familyNames maps familyId -> name, so a family recipe's badge can say which family.
function recipeCard(recipe, { badge = true, familyNames = {} } = {}) {
  return h("article", { class: "card recipe-card" },
    h("div", {},
      h("a", { class: "card-title", href: `#/recipe/${recipe.recipeId}`, text: recipe.title }),
      badge && recipe.visibility === "family"
        ? [" ", h("span", { class: "badge", text: familyNames[recipe.familyId] || "Family only" })] : null),
    h("div", { class: "muted", text: `by ${recipe.authorName} · ${formatDate(recipe.createdAt)}` }),
    recipe.description ? h("p", { class: "card-desc", text: recipe.description }) : null,
    h("div", { class: "muted", text: `${recipe.ingredients.length} ingredients · ${recipe.steps.length} steps` }),
  );
}

// cardOptions: by default the section heading says who can see the recipes, so cards skip the badge.
function recipeSection(title, subtitle, recipes, emptyState, cardOptions = { badge: false }) {
  return h("section", { class: "feed-section", "aria-label": title },
    h("div", { class: "section-head" },
      h("h2", {}, title, " ", h("span", { class: "count", text: String(recipes.length) })),
      subtitle ? h("span", { class: "muted", text: subtitle }) : null),
    recipes.length ? h("div", { class: "grid" }, recipes.map((r) => recipeCard(r, cardOptions))) : emptyState);
}

// ---------- families (shared by several pages) ----------

const MAX_FAMILIES = 10;  // keep in sync with backend/recipe_api/main.py

// The signed-in account's families, refreshed by pages that show them.
let myFamilies = [];

async function loadFamilies() {
  myFamilies = me?.familyIds?.length ? (await api("/families/mine")).families : [];
  return myFamilies;
}

function namesOf(families) {
  return Object.fromEntries(families.map((family) => [family.familyId, family.name]));
}

function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

async function feedView() {
  if (!me) {
    const { recipes } = await api("/public/recipes");
    return render(
      h("div", { class: "page-head" },
        h("div", {},
          h("h1", { text: "Public recipes" }),
          h("div", { class: "muted", text: "Sign in to write recipes, comment, and share with your family." }))),
      recipes.length
        ? h("div", { class: "grid" }, recipes.map((r) => recipeCard(r)))
        : h("div", { class: "empty" }, h("p", { text: "No recipes yet." })),
    );
  }

  const [feed, families] = await Promise.all([api("/recipes"), loadFamilies()]);
  // Recipes from every family you're in; with more than one family, each card says which.
  const familySection = families.length
    ? recipeSection("Family recipes",
        families.length === 1 ? `Only ${families[0].name} can see these` : `From your ${families.length} families`,
        feed.family,
        h("div", { class: "empty" },
          h("p", { text: "No family recipes yet." }),
          h("a", { class: "button", href: "#/new/family", text: "Share one with your family" })),
        { badge: families.length > 1, familyNames: namesOf(families) })
    : recipeSection("Family recipes", null, [],
        h("div", { class: "card invite-card" },
          h("div", {},
            h("h3", { text: "Keep recipes in the family" }),
            h("p", { class: "muted", text: "Start or join a family to share recipes only your family can see." })),
          h("a", { class: "button", href: "#/family", text: "Set up a family" })));

  render(
    h("div", { class: "page-head" },
      h("div", {},
        h("h1", { text: "Home" }),
        h("div", { class: "muted", text: "Every recipe you can see, newest first." })),
      h("a", { class: "button", href: "#/new", text: "New recipe" })),
    familySection,
    recipeSection("Public recipes", "Shared with everyone", feed.public,
      h("div", { class: "empty" },
        h("p", { text: "No public recipes yet." }),
        h("a", { class: "button", href: "#/new", text: "Write the first one" }))),
  );
}

function avatar(name) {
  return h("span", { class: "avatar", "aria-hidden": "true", text: (name || "?").charAt(0).toUpperCase() });
}

function commentNode(recipe, comment) {
  const canDelete = me && (me.userId === comment.authorId || me.userId === recipe.authorId);
  const mine = me && me.userId === comment.authorId;
  return h("div", { class: "comment" },
    h("div", { class: "comment-head" },
      avatar(comment.authorName),
      h("div", {},
        h("strong", { text: comment.authorName }),
        mine ? [" ", h("span", { class: "badge", text: "You" })] : null,
        recipe.authorId === comment.authorId ? [" ", h("span", { class: "badge", text: "Recipe author" })] : null,
        h("div", { class: "muted", text: formatDate(comment.createdAt) }))),
    h("p", { text: comment.text }),
    canDelete ? h("button", {
      class: "link",
      text: "Delete",
      onclick: async () => {
        if (!confirm("Delete this comment?")) return;
        await api(`/recipes/${recipe.recipeId}/comments/${comment.commentId}`, { method: "DELETE" });
        route();
      },
    }) : null,
  );
}

async function recipeView(id) {
  const base = me ? `/recipes/${id}` : `/public/recipes/${id}`;
  const [recipe, { comments }, families] = await Promise.all([
    api(base), api(`${base}/comments`), me ? loadFamilies() : [],
  ]);
  const isAuthor = me && me.userId === recipe.authorId;
  const familyName = namesOf(families)[recipe.familyId];

  // Moves the recipe from its family to Public (the same as Edit > Who can see it > Public).
  const publish = async () => {
    const where = familyName ? `${familyName}'s recipes` : "family recipes";
    if (!confirm(`Publish "${recipe.title}" to everyone?\n\nAnyone, signed in or not, will be able to see it ` +
                 `and comment. It moves from ${where} to Public recipes, where your family can still see it. ` +
                 "You can make it family-only again with Edit.")) return;
    const { title, description, ingredients, steps } = recipe;
    await api(`/recipes/${id}`, { method: "PUT", body: { title, description, ingredients, steps, visibility: "public" } });
    go(`#/recipe/${id}`, "Published. Everyone can see this recipe now.");
  };

  const commentBox = h("textarea", { maxlength: 2000, required: true, placeholder: "Share a tip or say thanks", "aria-label": "Comment" });
  const commentForm = me ? h("form", {
    onsubmit: async (event) => {
      event.preventDefault();
      await api(`/recipes/${id}/comments`, { method: "POST", body: { text: commentBox.value } });
      route();
    },
  }, commentBox, h("div", {}, h("button", { type: "submit", text: "Post comment" }))) :
    h("p", { class: "muted" },
      h("button", { type: "button", class: "link", text: "Sign in", onclick: requireSignIn }),
      " or ", h("a", { href: "#/signup", text: "create an account" }), " to comment.");

  render(
    h("div", { class: "page-head" },
      h("div", {},
        h("h1", { class: "recipe-title" }, recipe.title,
          recipe.visibility === "family"
            ? [" ", h("span", { class: "badge", text: familyName ? `Family only · ${familyName}` : "Family only" })]
            : null),
        h("div", { class: "muted", text: `by ${recipe.authorName} · ${formatDate(recipe.createdAt)}` })),
      isAuthor ? h("div", { class: "row" },
        recipe.visibility === "family"
          ? h("button", { type: "button", class: "secondary", text: "Publish to public", onclick: publish }) : null,
        h("a", { class: "button", href: `#/edit/${id}`, text: "Edit" }),
        h("button", {
          class: "danger",
          text: "Delete",
          onclick: async () => {
            if (!confirm("Delete this recipe and its comments?")) return;
            await api(`/recipes/${id}`, { method: "DELETE" });
            location.hash = "#/";
          },
        })) : null),
    recipe.description ? h("p", { class: "description", text: recipe.description }) : null,
    h("div", { class: "recipe-layout" },
      h("section", { class: "card" },
        h("h2", { text: "Ingredients" }),
        h("ul", { class: "ingredients" }, recipe.ingredients.map((item) => h("li", { text: item })))),
      h("section", { class: "card" },
        h("h2", { text: "Steps" }),
        h("ol", { class: "steps" }, recipe.steps.map((step) => h("li", { text: step }))))),
    h("section", { class: "card" },
      h("h2", { text: `Comments (${comments.length})` }),
      comments.map((c) => commentNode(recipe, c)),
      commentForm),
  );
}

// defaults preselect who can see a new recipe: #/new/family picks your first
// family, #/new/family/<familyId> that family.
async function recipeFormView(id, defaults = {}) {
  if (!me) return requireSignIn();
  const [existing, families] = await Promise.all([id ? api(`/recipes/${id}`) : null, loadFamilies()]);
  const lines = (list) => (list || []).join("\n");
  const title = h("input", { maxlength: 120, required: true, value: existing?.title || "" });
  const description = h("textarea", { maxlength: 2000 });
  description.value = existing?.description || "";
  const ingredients = h("textarea", { required: true, placeholder: "2 cups flour\n1 tsp cinnamon" });
  ingredients.value = lines(existing?.ingredients);
  const steps = h("textarea", { required: true, placeholder: "Preheat the oven to 350°F.\nWhisk the dry ingredients." });
  steps.value = lines(existing?.steps);
  // Option values are "public" or "family:<familyId>".
  const inFamily = (familyId) => families.some((f) => f.familyId === familyId);
  const leftFamily = existing?.visibility === "family" && !inFamily(existing.familyId);
  const visibility = h("select", {},
    h("option", { value: "public", text: "Public: anyone can see it" }),
    families.map((f) => h("option", { value: `family:${f.familyId}`, text: `Family only: ${f.name}` })),
    // Kept selectable so saving doesn't silently make it public; the API asks for a family you're in.
    leftFamily ? h("option", { value: `family:${existing.familyId}`, text: "Family only: a family you've left" }) : null,
    families.length ? null : h("option", { value: "family", disabled: true, text: "Family only (join or start a family first)" }),
  );
  if (existing) {
    visibility.value = existing.visibility === "family" ? `family:${existing.familyId}` : "public";
  } else if (inFamily(defaults.familyId)) {
    visibility.value = `family:${defaults.familyId}`;
  } else {
    visibility.value = defaults.family && families.length ? `family:${families[0].familyId}` : "public";
  }
  const toLines = (el) => el.value.split("\n").map((s) => s.trim()).filter(Boolean);

  render(
    h("h1", { text: existing ? "Edit recipe" : "New recipe" }),
    h("form", {
      class: "card",
      onsubmit: async (event) => {
        event.preventDefault();
        const [who, familyId] = visibility.value.split(":");
        const body = {
          title: title.value, description: description.value,
          ingredients: toLines(ingredients), steps: toLines(steps), visibility: who, familyId: familyId || null,
        };
        const saved = existing
          ? await api(`/recipes/${id}`, { method: "PUT", body })
          : await api("/recipes", { method: "POST", body });
        location.hash = `#/recipe/${saved.recipeId}`;
      },
    },
      h("label", {}, "Title", title),
      h("label", {}, "Description", h("span", { class: "hint", text: "Optional. A line or two about the dish." }), description),
      h("label", {}, "Ingredients", h("span", { class: "hint", text: "One per line." }), ingredients),
      h("label", {}, "Steps", h("span", { class: "hint", text: "One per line; they're numbered for you." }), steps),
      h("label", {}, "Who can see it",
        families.length > 1 ? h("span", { class: "hint", text: "A family recipe is seen only by the family you pick." }) : null,
        visibility),
      h("div", {}, h("button", { type: "submit", text: existing ? "Save changes" : "Publish recipe" })),
    ),
  );
}

// ---------- family pages ----------

function inviteLink(code) {
  return `${location.origin}${location.pathname}#/join/${code}`;
}

// Accepts a bare code or a pasted invite link.
function inviteCodeFrom(text) {
  return text.trim().split("/").pop().replace(/[^A-Za-z0-9]/g, "");
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // No Clipboard API access (older browsers, some in-app browsers and frames):
    // fall back to copying from a hidden, selected text field.
    const focused = document.activeElement;
    const buffer = h("textarea", { class: "copy-buffer", readonly: true, "aria-hidden": "true", tabindex: "-1" });
    buffer.value = text;
    document.body.append(buffer);
    buffer.select();
    let copied = false;
    try { copied = document.execCommand("copy"); } catch {}
    buffer.remove();
    focused?.focus?.();
    return copied;
  }
}

async function joinFamily(code) {
  const family = await api("/families/join", { method: "POST", body: { inviteCode: code } });
  await loadMe();
  go(`#/family/${family.familyId}`, `You joined ${family.name}!`);
}

function familyCard(family) {
  const names = family.members.map((m) => (m.userId === me.userId ? "You" : m.displayName));
  return h("article", { class: "card recipe-card" },
    h("div", {},
      h("a", { class: "card-title", href: `#/family/${family.familyId}`, text: family.name }),
      family.ownerId === me.userId ? [" ", h("span", { class: "badge", text: "Owner" })] : null),
    h("div", { class: "muted", text: plural(family.members.length, "member") }),
    h("p", { class: "card-desc", text: names.slice(0, 5).join(", ") + (names.length > 5 ? ", …" : "") }));
}

async function familiesView() {
  if (!me) return requireSignIn();
  const families = await loadFamilies();

  const name = h("input", { maxlength: 60, required: true, placeholder: "e.g. The Washingtons" });
  const code = h("input", { maxlength: 300, required: true, autocomplete: "off", placeholder: "8-character code, or the invite link" });
  const startCard = h("div", { class: "card" }, h("h2", { text: "Start a family" }),
    h("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        const family = await api("/families", { method: "POST", body: { name: name.value } });
        await loadMe();
        go(`#/family/${family.familyId}`, "Family created. Copy the invite link to add people.");
      },
    }, h("label", {}, "Family name", name), h("div", {}, h("button", { type: "submit", text: "Create family" }))));
  const joinCard = h("div", { class: "card" }, h("h2", { text: "Join a family" }),
    h("form", { onsubmit: (e) => { e.preventDefault(); joinFamily(inviteCodeFrom(code.value)); } },
      h("label", {}, "Invite code or link", code), h("div", {}, h("button", { type: "submit", text: "Join family" }))));

  render(
    h("div", { class: "page-head" },
      h("div", {},
        h("h1", { text: families.length ? "Your families" : "Family" }),
        h("div", { class: "muted", text: "A family links accounts so you can share recipes only they can see. You can be in more than one." }))),
    families.length ? h("div", { class: "grid" }, families.map(familyCard)) : null,
    families.length >= MAX_FAMILIES
      ? h("p", { class: "muted", text: `You're in ${MAX_FAMILIES} families, the most allowed. Leave one to join or start another.` })
      : [families.length ? h("h2", { text: "Join or start another family" }) : null,
         h("div", { class: "grid" }, joinCard, startCard)],
  );
}

async function familyDetailView(familyId) {
  if (!me) return requireSignIn();
  const families = await loadFamilies();
  const family = families.find((f) => f.familyId === familyId);
  if (!family) {
    return render(
      h("h1", { text: "Family not found" }),
      h("p", { class: "muted", text: "You're not in this family, or it no longer exists." }),
      h("p", {}, h("a", { href: "#/family", text: "← Your families" })));
  }
  const { recipes } = await api(`/families/${familyId}/recipes`);
  const isOwner = family.ownerId === me.userId;
  const others = family.members.filter((m) => m.userId !== me.userId);
  const status = h("p", { class: "muted", role: "status" });

  // Owners rename the family in place: the heading swaps for a small form.
  const heading = h("div", { class: "title-row" },
    h("h1", { text: family.name }),
    isOwner ? h("button", { type: "button", class: "link", text: "Rename", onclick: () => startRename() }) : null);
  function startRename() {
    const input = h("input", { maxlength: 60, required: true, value: family.name, "aria-label": "Family name" });
    const form = h("form", {
      class: "rename-form",
      onsubmit: async (e) => {
        e.preventDefault();
        await api(`/families/${familyId}`, { method: "PUT", body: { name: input.value } });
        go(location.hash, "Family name saved.");
      },
    }, input,
      h("button", { type: "submit", text: "Save" }),
      h("button", { type: "button", class: "secondary", text: "Cancel", onclick: () => form.replaceWith(heading) }));
    heading.replaceWith(form);
    input.select();
  }

  const copy = async (text, message) => {
    status.textContent = (await copyText(text)) ? message : `Couldn't copy automatically. Select and copy it: ${text}`;
  };
  const link = family.inviteCode ? inviteLink(family.inviteCode) : null;
  const inviteCard = isOwner ? h("div", { class: "card invite-code" },
    h("div", { class: "muted", text: "Invite people with the code or the link. Click the code to copy it." }),
    h("div", { class: "row" },
      h("button", {
        type: "button", class: "code", text: family.inviteCode, "aria-label": `Copy invite code ${family.inviteCode}`,
        onclick: () => copy(family.inviteCode, "Invite code copied."),
      }),
      h("button", { type: "button", text: "Copy invite link", onclick: () => copy(link, "Invite link copied. Paste it into a text or email.") }),
      // Phones get the system share sheet (Messages, WhatsApp, email...).
      navigator.share ? h("button", {
        type: "button", class: "secondary", text: "Share…",
        onclick: () => navigator.share({ title: `Join ${family.name} on Family Foodie Book`, url: link }).catch(() => {}),
      }) : null,
      h("button", {
        type: "button", class: "link", text: "Make a new code",
        onclick: async () => {
          if (!confirm("The old code and link will stop working. Continue?")) return;
          const updated = await api(`/families/${familyId}/invite-code`, { method: "POST" });
          const copied = await copyText(updated.inviteCode);
          go(location.hash, copied ? "New code made and copied. The old code and link no longer work."
                                   : "New code made. The old code and link no longer work.");
        },
      })),
    h("div", { class: "invite-link", text: link }),
    status) : null;

  const leave = async () => {
    const next = others.length ? [...others].sort((a, b) => a.joinedAt.localeCompare(b.joinedAt))[0] : null;
    const consequence = !next
      ? "You're the last member, so the family and its invite code will be deleted."
      : isOwner ? `${next.displayName} will become the owner.` : "";
    if (!confirm(`Leave ${family.name}? Its family-only recipes will no longer be visible to you. ${consequence}`)) return;
    await api(`/families/${familyId}/leave`, { method: "POST" });
    await loadMe();
    go("#/family", `You left ${family.name}.`);
  };

  render(
    h("p", { class: "back-link" }, h("a", { href: "#/family", text: "← Your families" })),
    h("div", { class: "page-head" },
      h("div", {}, heading, h("div", { class: "muted", text: plural(family.members.length, "member") })),
      h("a", { class: "button", href: `#/new/family/${familyId}`, text: "Share a family recipe" })),
    recipeSection("Family recipes", `Only ${family.name} can see these`, recipes,
      h("div", { class: "empty" },
        h("p", { text: "No family recipes yet." }),
        h("a", { class: "button", href: `#/new/family/${familyId}`, text: "Share the first one" }))),
    h("h2", { text: `Members (${family.members.length})` }),
    // Only the owner gets the invite code back from the API (inviteCode is null for everyone else).
    inviteCard,
    h("div", { class: "card" }, family.members.map((member) => h("div", { class: "member" },
      h("div", {},
        avatar(member.displayName),
        member.displayName,
        member.userId === me.userId ? [" ", h("span", { class: "badge", text: "You" })] : null,
        member.userId === family.ownerId ? [" ", h("span", { class: "badge", text: "Owner" })] : null),
      isOwner && member.userId !== me.userId ? h("button", {
        class: "danger",
        text: "Remove",
        onclick: async () => {
          if (!confirm(`Remove ${member.displayName} from ${family.name}?`)) return;
          await api(`/families/${familyId}/members/${member.userId}`, { method: "DELETE" });
          route();
        },
      }) : null,
    ))),
    h("p", {}, h("button", { class: "danger", text: "Leave family", onclick: leave })),
  );
}

// Where an invite link lands: #/join/<code>.
async function joinView(code) {
  if (!me) return requireSignIn("Sign in or create an account to join the family.");
  let preview;
  try {
    preview = await api(`/families/invite/${encodeURIComponent(code)}`);
  } catch (error) {
    return render(authPage("This invite isn't valid",
      `${error.message} Ask the family's owner for a new link.`,
      h("p", { class: "auth-switch" }, h("a", { href: "#/family", text: "Go to your families" }))));
  }
  if (preview.alreadyMember) return go(`#/family/${preview.familyId}`, `You're already in ${preview.name}.`);
  const button = h("button", { type: "button", text: `Join ${preview.name}` });
  button.addEventListener("click", async () => {
    button.disabled = true;
    try { await joinFamily(code); } finally { button.disabled = false; }
  });
  render(authPage(`Join ${preview.name}?`,
    `You've been invited to ${preview.name} (${plural(preview.memberCount, "member")}). ` +
    "Members can see each other's family-only recipes. You stay in any other families you're in.",
    h("div", { class: "row" }, button, h("a", { href: "#/", text: "Not now" }))));
}

async function profileView() {
  if (!me) return requireSignIn();
  const name = h("input", { maxlength: 40, required: true, value: me.displayName });
  render(
    h("h1", { text: "Your profile" }),
    h("p", { class: "muted", text: `Signed in as ${me.email}` }),
    h("form", {
      class: "card",
      onsubmit: async (event) => {
        event.preventDefault();
        me = await api("/me", { method: "PUT", body: { displayName: name.value } });
        renderNav();
        showNotice("Display name saved.");
      },
    }, h("label", {}, "Display name", name), h("div", {}, h("button", { type: "submit", text: "Save" }))),
    h("p", {}, h("a", { href: "#/appearance", text: "Change how Family Foodie Book looks →" })),
  );
}

// ---------- appearance ----------

let preferenceSaveTimer;

// statusEl is optional: background saves (like a location re-check) stay quiet.
function savePreferences(next, statusEl) {
  const say = (message) => { if (statusEl) statusEl.textContent = message; };
  const saved = Theme.save(next);
  if (!me) {
    say("Saved on this device.");
    return;
  }
  say("Saving…");
  clearTimeout(preferenceSaveTimer);
  // Wait for a pause so dragging a color picker doesn't send a request per pixel.
  preferenceSaveTimer = setTimeout(async () => {
    try {
      me = await api("/me/preferences", { method: "PUT", body: saved });
      say("Saved to your account.");
    } catch (error) {
      say(error.message);
    }
  }, 700);
}

// When "Follow this device's location" is on and permission was already
// granted, re-check the hemisphere each time the app opens. This never shows
// a permission prompt; that only happens when the user turns the setting on.
async function followDeviceLocation() {
  const prefs = Theme.get();
  if (!prefs.followLocation || (await Theme.locationPermission()) !== "granted") return;
  try {
    const hemisphere = await Theme.detectHemisphere();
    if (hemisphere !== Theme.get().hemisphere) {
      savePreferences({ ...Theme.get(), hemisphere });
      if ((location.hash || "").startsWith("#/appearance")) route();
    }
  } catch {
    // Keep the last known hemisphere if the device can't answer right now.
  }
}

function appearanceView() {
  const status = h("span", { class: "muted", role: "status" });
  const controls = [];  // [element, (prefs) => void] pairs refreshed after every change
  const update = (changes) => {
    savePreferences({ ...Theme.get(), ...changes }, status);
    refresh();
  };
  const refresh = () => controls.forEach(([, sync]) => sync(Theme.get()));

  // options: [value, label] where label may be a function of the current prefs.
  // extra: more changes to make alongside picking an option.
  function segmented(label, hint, key, options, extra = {}) {
    const buttons = options.map(([value, text]) => {
      const button = h("button", { type: "button", onclick: () => update({ [key]: value, ...extra }) });
      controls.push([button, (p) => {
        button.textContent = typeof text === "function" ? text(p) : text;
        button.setAttribute("aria-pressed", String(p[key] === value));
      }]);
      return button;
    });
    return h("div", { class: "setting-row" },
      h("div", { class: "setting-label" }, label, hint ? h("span", { class: "hint", text: hint }) : null),
      h("div", { class: "segmented", role: "group", "aria-label": label }, buttons));
  }

  const autoLabel = (p) => `Auto (now ${capitalize(Theme.seasonFor(p.hemisphere))})`;

  const follow = h("input", { type: "checkbox" });
  follow.addEventListener("change", async () => {
    if (!follow.checked) return update({ followLocation: false });
    status.textContent = "Checking this device's location…";
    try {
      const hemisphere = await Theme.detectHemisphere();
      update({ followLocation: true, hemisphere });
    } catch (error) {
      follow.checked = false;
      status.textContent = error.message;
    }
  });
  controls.push([follow, (p) => { follow.checked = p.followLocation; }]);
  const seasonItems = Theme.SEASONS.map((season) => {
    const tag = h("span", { class: "selected-tag" });
    const card = h("button", {
      type: "button",
      class: "season-preview",
      "data-season": season,
      "aria-label": `Use the ${season} theme`,
      onclick: () => update({ season }),
    },
      h("div", { class: "swatch-header", "aria-hidden": "true" }),
      h("div", { class: "swatch-body", "aria-hidden": "true" },
        h("div", { class: "swatch-line" }),
        h("div", { class: "swatch-line short" }),
        h("span", { class: "swatch-button", text: "Button" })),
    );
    const picker = h("input", { type: "color", "aria-label": `${capitalize(season)} accent color` });
    const reset = h("button", { type: "button", class: "link", text: "Default color" });
    const accentsWith = (color) => {
      const accents = { ...Theme.get().accents };
      if (color) accents[season] = color; else delete accents[season];
      return accents;
    };
    // "input" fires while dragging: preview only. "change" fires on release: save.
    picker.addEventListener("input", () => {
      Theme.paintAccent(card, picker.value);
      if (Theme.resolvedSeason() === season) Theme.apply({ ...Theme.get(), accents: accentsWith(picker.value) });
    });
    picker.addEventListener("change", () => update({ accents: accentsWith(picker.value) }));
    reset.addEventListener("click", () => update({ accents: accentsWith(null) }));

    controls.push([card, (p) => {
      const active = Theme.resolvedSeason(p) === season;
      card.dataset.mode = Theme.resolvedMode(p);
      card.setAttribute("aria-pressed", String(active));
      tag.textContent = active ? (p.season === "auto" ? "Active (auto)" : "Active") : "";
      Theme.paintAccent(card, null);
      const baseAccent = getComputedStyle(card).getPropertyValue("--accent").trim();
      Theme.paintAccent(card, p.accents[season]);
      picker.value = p.accents[season] || baseAccent;
      reset.hidden = !p.accents[season];
    }]);
    return h("div", { class: "season-item" },
      card,
      h("div", { class: "accent-row" }, picker, h("span", { class: "hint", text: "Accent" }), reset, tag));
  });

  const decorations = h("input", { type: "checkbox", onchange: () => update({ decorations: decorations.checked }) });
  controls.push([decorations, (p) => { decorations.checked = p.decorations; }]);

  render(
    h("div", { class: "page-head" },
      h("div", {},
        h("h1", { text: "Appearance" }),
        h("div", { class: "muted", text: me
          ? "Your choices are saved to your account and follow you to other devices."
          : "Your choices are saved on this device. Sign in to keep them on every device." })),
      status),
    h("section", { class: "card settings-section" },
      h("h2", { text: "Season" }),
      segmented("Theme", "Auto switches with the calendar for your hemisphere.", "season",
        [["auto", autoLabel], ...Theme.SEASONS.map((s) => [s, capitalize(s)])]),
      h("div", { class: "setting-row" },
        h("div", { class: "setting-label" }, "Follow this device's location",
          h("span", { class: "hint", text: "Picks your hemisphere from where this device is, and re-checks each time the app opens. Only \"north\" or \"south\" is saved, never your location." })),
        h("label", { class: "switch" }, follow, "Use location")),
      // Choosing a hemisphere by hand turns location following off, so it isn't overridden later.
      segmented("Hemisphere", "Seasons are flipped south of the equator.", "hemisphere",
        [["north", "Northern"], ["south", "Southern"]], { followLocation: false }),
      h("p", { class: "hint", text: "Pick a season below, and choose your own accent color for each one. Colors that would be hard to read are adjusted automatically." }),
      h("div", { class: "season-grid" }, seasonItems)),
    h("section", { class: "card settings-section" },
      h("h2", { text: "Display" }),
      segmented("Light or dark", "Match device follows your computer or phone setting.", "mode",
        [["system", "Match device"], ["light", "Light"], ["dark", "Dark"]]),
      segmented("Heading font", null, "font", [["serif", "Cookbook serif"], ["sans", "Modern sans"]]),
      segmented("Text size", null, "textSize", [["normal", "Normal"], ["large", "Large"]]),
      h("div", { class: "setting-row" },
        h("div", { class: "setting-label" }, "Seasonal decorations",
          h("span", { class: "hint", text: "Colored header, season emoji and a soft glow." })),
        h("label", { class: "switch" }, decorations, "Show"))),
    h("p", {}, h("button", {
      type: "button",
      class: "secondary",
      text: "Reset to defaults",
      onclick: () => update({ ...Theme.DEFAULTS, accents: {} }),
    })),
  );
  refresh();
}

// ---------- sign in / create account ----------

function authPage(title, subtitle, ...content) {
  return h("div", { class: "auth-wrap" },
    h("div", { class: "card auth-card" },
      h("div", { class: "auth-motif", "aria-hidden": "true" }),
      h("h1", { text: title }),
      subtitle ? h("p", { class: "muted", text: subtitle }) : null,
      ...content));
}

function emailInput(value = "") {
  return h("input", { type: "email", autocomplete: "username", required: true, maxlength: 254, value });
}

function passwordInput(autocomplete) {
  const input = h("input", { type: "password", autocomplete, required: true, maxlength: 256 });
  const toggle = h("button", {
    type: "button",
    class: "link",
    text: "Show",
    "aria-label": "Show password",
    onclick: () => {
      const showing = input.type === "text";
      input.type = showing ? "password" : "text";
      toggle.textContent = showing ? "Show" : "Hide";
      toggle.setAttribute("aria-label", showing ? "Show password" : "Hide password");
    },
  });
  return { input, field: h("div", { class: "password-field" }, input, toggle) };
}

// Live checklist under a new-password field.
function passwordRules(input) {
  const items = PASSWORD_RULES.map(([, text]) => h("li", { text }));
  const sync = () => PASSWORD_RULES.forEach(([rule], i) => items[i].classList.toggle("met", rule.test(input.value)));
  input.addEventListener("input", sync);
  sync();
  return h("ul", { class: "password-rules", "aria-label": "Password requirements" }, items);
}

function errorBox() {
  return h("p", { class: "form-error", role: "alert", hidden: true });
}

// Runs an auth form submit: disables the button, shows errors in the box.
async function submitting(form, errorEl, work) {
  const button = form.querySelector('button[type="submit"]');
  errorEl.hidden = true;
  button.disabled = true;
  try {
    await work();
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  } finally {
    button.disabled = false;
  }
}

function afterSignIn(message) {
  const returnTo = session.get("returnTo");
  session.remove("returnTo");
  renderNav();
  go(returnTo && !returnTo.startsWith("#/sign") ? returnTo : "#/", message);
}

// The password typed on the sign-up page is kept only in memory, so the
// account can be signed in right after the email is confirmed.
let pendingPassword = null;

function signInView() {
  if (me) return go("#/");
  const pending = session.get("pendingSignup");
  const email = emailInput(pending?.email || "");
  const password = passwordInput("current-password");
  const remember = h("input", { type: "checkbox" });
  const error = errorBox();
  const form = h("form", {
    onsubmit: (event) => {
      event.preventDefault();
      submitting(form, error, async () => {
        try {
          await signIn(email.value, password.input.value, remember.checked);
        } catch (err) {
          if (err.code === "UserNotConfirmedException") {
            session.set("pendingSignup", { ...(pending || {}), email: email.value.trim() });
            pendingPassword = password.input.value;
            return go("#/confirm", "Confirm your email to finish creating your account.");
          }
          throw err;
        }
        afterSignIn(`Welcome back, ${me.displayName}!`);
      });
    },
  },
    h("label", {}, "Email", email),
    h("label", {}, h("span", { class: "label-row" }, "Password", h("a", { href: "#/forgot", text: "Forgot password?" })),
      password.field),
    h("label", { class: "switch" }, remember, "Keep me signed in on this device"),
    error,
    h("button", { type: "submit", text: "Sign in" }));
  render(authPage("Welcome back", "Sign in to your Family Foodie Book account.", form,
    h("p", { class: "auth-switch" }, "New here? ", h("a", { href: "#/signup", text: "Create an account" }))));
  email.focus();
}

function signUpView() {
  if (me) return go("#/");
  const name = h("input", { autocomplete: "name", required: true, maxlength: 40, placeholder: "e.g. Grandma Rose" });
  const email = emailInput();
  const password = passwordInput("new-password");
  const error = errorBox();
  const form = h("form", {
    onsubmit: (event) => {
      event.preventDefault();
      submitting(form, error, async () => {
        const failed = PASSWORD_RULES.filter(([rule]) => !rule.test(password.input.value));
        if (failed.length) throw new Error(`Your password needs: ${failed.map(([, t]) => t.toLowerCase()).join(", ")}.`);
        const result = await cognito("SignUp", {
          Username: email.value.trim(),
          Password: password.input.value,
          UserAttributes: [
            { Name: "email", Value: email.value.trim() },
            { Name: "name", Value: name.value.trim() },
          ],
        });
        session.set("pendingSignup", {
          email: email.value.trim(),
          name: name.value.trim(),
          destination: result.CodeDeliveryDetails?.Destination,
        });
        pendingPassword = password.input.value;
        if (result.UserConfirmed) {
          await signIn(email.value, pendingPassword, false);
          pendingPassword = null;
          return afterSignIn(`Welcome to Family Foodie Book, ${me.displayName}!`);
        }
        go("#/confirm");
      });
    },
  },
    h("label", {}, "Your name", h("span", { class: "hint", text: "Shown on your recipes and comments. You can change it later." }), name),
    h("label", {}, "Email", email),
    h("label", {}, "Password", password.field),
    passwordRules(password.input),
    error,
    h("button", { type: "submit", text: "Create account" }));
  render(authPage("Create your account", "Save recipes, comment, and share with your family.", form,
    h("p", { class: "auth-switch" }, "Already have an account? ", h("a", { href: "#/signin", text: "Sign in" }))));
  name.focus();
}

function confirmView() {
  const pending = session.get("pendingSignup");
  if (!pending?.email) return go("#/signup");
  const code = h("input", { inputmode: "numeric", autocomplete: "one-time-code", required: true, maxlength: 10,
                            placeholder: "6-digit code" });
  const error = errorBox();
  const form = h("form", {
    onsubmit: (event) => {
      event.preventDefault();
      submitting(form, error, async () => {
        await cognito("ConfirmSignUp", { Username: pending.email, ConfirmationCode: code.value.trim() });
        if (!pendingPassword) return go("#/signin", "Email confirmed! Sign in to get started.");
        await signIn(pending.email, pendingPassword, false);
        pendingPassword = null;
        afterSignIn(`Welcome to Family Foodie Book, ${me.displayName}!`);
      });
    },
  },
    h("label", {}, "Confirmation code", code),
    config.localDev ? h("p", { class: "hint", text: "Local dev server: no email is sent. The code is printed in the server's terminal." }) : null,
    error,
    h("button", { type: "submit", text: "Confirm email" }));
  const resend = h("button", {
    type: "button",
    class: "link",
    text: "Send a new code",
    onclick: async () => {
      error.hidden = true;
      try {
        await cognito("ResendConfirmationCode", { Username: pending.email });
        showNotice("A new code is on its way.");
      } catch (err) {
        error.textContent = err.message;
        error.hidden = false;
      }
    },
  });
  render(authPage("Check your email",
    `We sent a code to ${pending.destination || pending.email}. Enter it below to finish creating your account.`,
    form, h("p", { class: "auth-switch" }, "Didn't get it? ", resend)));
  code.focus();
}

function forgotView() {
  let email = "";
  const step1 = () => {
    const emailField = emailInput(session.get("pendingSignup")?.email || "");
    const error = errorBox();
    const form = h("form", {
      onsubmit: (event) => {
        event.preventDefault();
        submitting(form, error, async () => {
          email = emailField.value.trim();
          const result = await cognito("ForgotPassword", { Username: email });
          step2(result.CodeDeliveryDetails?.Destination);
        });
      },
    }, h("label", {}, "Email", emailField), error, h("button", { type: "submit", text: "Send reset code" }));
    render(authPage("Reset your password", "We'll email you a code to choose a new password.", form,
      h("p", { class: "auth-switch" }, h("a", { href: "#/signin", text: "Back to sign in" }))));
    emailField.focus();
  };
  const step2 = (destination) => {
    const code = h("input", { inputmode: "numeric", autocomplete: "one-time-code", required: true, maxlength: 10 });
    const password = passwordInput("new-password");
    const error = errorBox();
    const form = h("form", {
      onsubmit: (event) => {
        event.preventDefault();
        submitting(form, error, async () => {
          await cognito("ConfirmForgotPassword", {
            Username: email, ConfirmationCode: code.value.trim(), Password: password.input.value,
          });
          go("#/signin", "Password changed. Sign in with your new password.");
        });
      },
    },
      h("label", {}, "Code", code),
      config.localDev ? h("p", { class: "hint", text: "Local dev server: no email is sent. The code is printed in the server's terminal." }) : null,
      h("label", {}, "New password", password.field),
      passwordRules(password.input),
      error,
      h("button", { type: "submit", text: "Change password" }));
    // Cognito answers the same whether or not the account exists, so this doesn't reveal who has one.
    render(authPage("Choose a new password",
      `If an account exists for ${email}, we sent a code to ${destination || "it"}.`, form,
      h("p", { class: "auth-switch" }, h("a", { href: "#/signin", text: "Back to sign in" }))));
    code.focus();
  };
  step1();
}

// ---------- routing ----------

async function route() {
  routeSeq += 1;
  showNotice(flashMessage);
  flashMessage = "";
  renderNav();
  const [, page, id, extra] = (location.hash || "#/").split("/");
  view.classList.toggle("narrow", ["new", "edit", "profile"].includes(page));
  try {
    if (page === "signin") signInView();
    else if (page === "signup") signUpView();
    else if (page === "confirm") confirmView();
    else if (page === "forgot") forgotView();
    else if (page === "recipe" && id) await recipeView(id);
    else if (page === "new") await recipeFormView(null, { family: id === "family", familyId: extra });
    else if (page === "edit" && id) await recipeFormView(id);
    else if (page === "family" && id) await familyDetailView(id);
    else if (page === "family") await familiesView();
    else if (page === "join" && id) await joinView(decodeURIComponent(id));
    else if (page === "profile") await profileView();
    else if (page === "appearance") appearanceView();
    else await feedView();
  } catch (error) {
    if (!(error instanceof StaleRoute)) showNotice(error.message);
  }
}

// Errors thrown from form and button handlers end up here.
window.addEventListener("unhandledrejection", (event) => {
  if (event.reason instanceof StaleRoute) return event.preventDefault();
  showNotice(event.reason?.message || "Something went wrong.");
});

async function start() {
  try {
    config = await (await fetch("config.json", { cache: "no-store" })).json();
    await loadMe();
  } catch (error) {
    showNotice(error.message);
  }
  window.addEventListener("hashchange", route);
  await route();
  followDeviceLocation();
}

start();
