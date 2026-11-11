"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const state = { bearer: "", csrf: "", view: "overview", generation: 0, authenticated: false, secretTimer: null };
  const descriptions = {
    overview: "Usage and safe runtime settings. This interface is an initial control plane, not a production-readiness certification.",
    teams: "Organize ownership and enforce spending limits. Budget behavior is defined by the gateway's accounting policy.",
    users: "Manage known users and their team membership. SSO is handled by your identity provider.",
    providers: "Register trusted provider endpoints. Provider credentials are write-only and are never displayed by this interface.",
    keys: "Issue scoped, expiring credentials. Raw keys appear once at creation or rotation.",
    audit: "Administrative and request metadata. Prompts and model responses should not be included in these records.",
    prompts: "Version prompts explicitly, preview literal variables and save deterministic evaluation reports.",
    evaluation: "Run bounded, deterministic checks on supplied outputs. No provider requests are made."
  };
  const fields = {
    teams: [
      { name: "name", label: "Team name", required: true },
      { name: "budget_usd", label: "Monthly budget (USD)", type: "number", min: "0", step: "0.01", required: true, value: "100" }
    ],
    users: [
      { name: "email", label: "Email", type: "email", required: true },
      { name: "team_id", label: "Team ID (optional)", help: "For keys, this must match the selected user’s current team." },
      { name: "role", label: "Role", options: ["member", "admin"], value: "member", help: "Member: own access only. Administrator: privileged organization management. API key permissions further restrict either role; they never grant a user a higher role." },
      { name: "budget_usd", label: "Monthly budget (USD)", type: "number", min: "0", step: "0.01", required: true, value: "25" }
    ],
    providers: [
      { name: "name", label: "Provider name", required: true },
      { name: "kind", label: "Adapter", options: ["openai", "anthropic", "azure_openai"], value: "openai" },
      { name: "base_url", label: "Trusted provider base URL", type: "url", required: true, value: "https://api.openai.com/v1" },
      { name: "api_key", label: "Provider API key", type: "password", required: true, help: "Stored encrypted by the gateway. Use only credentials approved for this deployment." },
      { name: "models", label: "Model aliases and prices (JSON object)", type: "json", required: true, value: '{"example-model":{"upstream_model":"provider-model-id","input_cost_per_million":1,"output_cost_per_million":3}}', help: "Replace the example with a real provider model and current prices. Prices are not automatically discovered." },
      { name: "enabled", label: "Enable provider", type: "checkbox", value: true }
    ],
    keys: [
      { name: "name", label: "Key name", required: true },
      { name: "kind", label: "Credential kind", options: ["api", "agent"], value: "api", help: "API keys call inference directly. Agent credentials exchange for short-lived relay access tokens." },
      { name: "user_id", label: "User ID", required: true },
      { name: "team_id", label: "Team ID (optional)", help: "For keys, this must match the selected user’s current team." },
      { name: "expires_in_days", label: "Expires in days", type: "number", min: "1", max: "365", required: true, value: "30" },
      { name: "rpm", label: "Requests per minute", type: "number", min: "1", required: true, value: "60" },
      { name: "tpm", label: "Token allowance per minute", type: "number", min: "1", required: true, value: "10000" },
      { name: "permissions", label: "Credential permissions (JSON array)", type: "json", required: true, value: '["inference"]', help: "inference: call models; account: manage only this key; admin: organization administration (administrator user required); provisioning: SCIM (administrator user required). Agent credentials support only [\"inference\"]. Grant only what the client needs." },
      { name: "allowed_models", label: "Allowed model aliases (JSON array)", type: "json", required: true, value: '["example-model"]', help: "Use explicit configured aliases. Review API policy before using an empty list." }
    ]
  };
  function message(text) { $("status").textContent = text; }
  function showError(error) {
    $("error").textContent = error instanceof Error ? error.message : String(error);
    $("error").hidden = false;
  }
  function clearError() { $("error").hidden = true; $("error").textContent = ""; }
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function safeValue(value, field = "") {
    if (/secret|password|api_key|encrypted|credential|key_hash|^digest$|^access_token$|^refresh_token$|^csrf_token$/i.test(field)) return "[redacted]";
    if (value === null || value === undefined) return "—";
    if (typeof value === "object") {
      if (Array.isArray(value)) return value.map((item) => safeValue(item)).join(", ");
      return Object.entries(value).map(([key, item]) => `${key}: ${safeValue(item, key)}`).join("; ");
    }
    return String(value);
  }
  async function api(path, options = {}) {
    const method = options.method || "GET";
    const headers = { Accept: "application/json" };
    if (state.bearer) headers.Authorization = `Bearer ${state.bearer}`;
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    if (!["GET", "HEAD", "OPTIONS"].includes(method) && state.csrf) headers["X-CSRF-Token"] = state.csrf;
    const response = await fetch(path, {
      method, headers, credentials: "same-origin", cache: "no-store",
      body: options.body === undefined ? undefined : JSON.stringify(options.body)
    });
    let data = null;
    const text = await response.text();
    if (text) {
      try { data = JSON.parse(text); } catch (_) { data = null; }
    }
    if (!response.ok) {
      const detail = data?.detail || data?.error?.message || data?.error;
      const explanation = typeof detail === "string" ? detail.slice(0, 1500) : (Array.isArray(detail) ? "Check the supplied fields and their types." : response.statusText);
      throw new Error(`${response.status}: ${explanation || "Request failed"}`);
    }
    return data;
  }
  function records(data) {
    if (Array.isArray(data)) return data;
    if (Array.isArray(data?.items)) return data.items;
    if (Array.isArray(data?.data)) return data.data;
    if (Array.isArray(data?.[state.view])) return data[state.view];
    return data ? [data] : [];
  }
  function clearSecret() {
    clearTimeout(state.secretTimer);
    state.secretTimer = null;
    $("new-secret").value = "";
    $("copy-status").textContent = "";
  }
  function showSecret(data) {
    const raw = data?.key || data?.api_key || data?.credential || data?.access_token;
    if (typeof raw !== "string") return;
    clearSecret();
    $("new-secret").value = raw;
    $("secret-dialog").showModal();
    state.secretTimer = setTimeout(() => $("secret-dialog").close(), 120000);
  }
  async function keyAction(id, action, button) {
    const text = action === "revoke" ? "Revoke this API key? Applications using it will lose access." : "Rotate this API key? The old key is revoked immediately. Save the returned credential and update dependent clients. Expiration is not extended.";
    if (!window.confirm(text)) return;
    button.disabled = true;
    clearError();
    try {
      const data = await api(`/admin/api/keys/${encodeURIComponent(id)}/${action}`, { method: "POST" });
      if (!state.authenticated) return;
      showSecret(data);
      message(action === "revoke" ? "Key revoked." : "Key rotated. Save the new credential.");
      await loadView();
    } catch (error) { showError(error); }
    finally { button.disabled = false; }
  }
  async function updateResource(resource, id, payload, button) {
    const change = "enabled" in payload ? (payload.enabled ? "enable" : "disable") : "change the monthly budget for";
    if (!window.confirm(`Confirm: ${change} this ${resource.slice(0, -1)}? This affects access or spending limits immediately.`)) return;
    button.disabled = true; clearError();
    try {
      await api(`/admin/api/${resource}/${encodeURIComponent(id)}`, { method: "PATCH", body: payload });
      if (state.authenticated) { message("Updated."); if (state.view === resource) await loadView(); }
    } catch (error) { showError(error); }
    finally { button.disabled = false; }
  }
  function renderTable(data) {
    const container = $("table-wrap"); container.replaceChildren();
    const rows = records(data);
    container.append(node("p", "Showing up to 100 records. Use the API limit/offset parameters for larger exports.", "empty"));
    if (!rows.length) { container.append(node("p", "No records yet.", "empty")); return; }
    const keys = [...new Set(rows.flatMap((row) => Object.keys(row)))].filter((key) => !/secret|password|api_key|encrypted|credential|key_hash|^digest$|^access_token$|^refresh_token$|^csrf_token$/i.test(key));
    const table = node("table"); const caption = node("caption", `${state.view} records`); caption.className = "sr-only"; table.append(caption);
    const head = node("thead"); const headRow = node("tr");
    keys.forEach((key) => { const th = node("th", key.replaceAll("_", " ")); th.scope = "col"; headRow.append(th); });
    if (["keys", "teams", "users", "providers"].includes(state.view)) { const th = node("th", "Actions"); th.scope = "col"; headRow.append(th); }
    head.append(headRow); table.append(head);
    const body = node("tbody");
    rows.forEach((row) => {
      const tr = node("tr"); keys.forEach((key) => tr.append(node("td", safeValue(row[key], key))));
      if (state.view === "keys") {
        const td = node("td");
        if (row.id && !row.revoked_at && !row.revoked) ["rotate", "revoke"].forEach((action) => {
          const button = node("button", action === "rotate" ? "Rotate" : "Revoke", action === "revoke" ? "danger" : "");
          button.type = "button"; button.addEventListener("click", () => keyAction(row.id, action, button)); td.append(button);
        });
        tr.append(td);
      }
      if (["teams", "users", "providers"].includes(state.view)) {
        const resource = state.view; const td = node("td");
        if (row.id) {
          const toggle = node("button", row.enabled === false ? "Enable" : "Disable", row.enabled === false ? "" : "danger");
          toggle.type = "button";
          toggle.addEventListener("click", () => updateResource(resource, row.id, { enabled: row.enabled === false }, toggle));
          td.append(toggle);
          if (resource === "users") {
            const sessions = node("button", "Revoke sessions", "danger"); sessions.type = "button";
            sessions.addEventListener("click", () => sessionAction(row.id, sessions)); td.append(sessions);
            if (!row.identity_linked) {
              const link = node("button", "Link SSO identity"); link.type = "button";
              link.addEventListener("click", () => identityAction(row, link)); td.append(link);
            }
          }
          if (["teams", "users"].includes(resource)) {
            const budget = node("button", "Set budget"); budget.type = "button";
            budget.addEventListener("click", () => {
              const answer = window.prompt("Monthly budget in USD (nonnegative number). Leave blank for no budget limit.", row.budget_usd == null ? "" : String(row.budget_usd));
              if (answer === null) return;
              const amount = answer.trim() === "" ? null : Number(answer);
              if (amount !== null && (!Number.isFinite(amount) || amount < 0)) { showError(new Error("Enter a nonnegative budget.")); return; }
              updateResource(resource, row.id, { budget_usd: amount }, budget);
            });
            td.append(budget);
          }
        }
        tr.append(td);
      }
      body.append(tr);
    });
    table.append(body); container.append(table);
  }
  async function sessionAction(userId, button) {
    const scope = userId ? `user ${userId}` : "EVERY user, including yourself";
    if (!window.confirm(`Revoke all browser sessions for ${scope}? This does not revoke API keys or prevent a new SSO login.`)) return;
    button.disabled = true; clearError();
    try {
      const path = userId ? `/admin/api/users/${encodeURIComponent(userId)}/sessions/revoke` : "/admin/api/sessions/revoke-all";
      const result = await api(path, { method: "POST", body: { confirm: true } });
      message(`Revoked ${result.revoked_sessions} browser sessions. API keys are unchanged.`);
    } catch (error) { showError(error); }
    finally { button.disabled = false; }
  }
  async function identityAction(row, button) {
    const issuer = window.prompt("Exact configured OIDC issuer URL. Verify the identity in your trusted IdP administration console, not by email matching.");
    if (issuer === null) return;
    const subject = window.prompt(`Exact IdP subject (sub) to link to ${row.email || row.id}. This grants that identity access to this account; SSO group mapping determines role/team.`);
    if (subject === null) return;
    if (!window.confirm(`I verified this identity with the trusted IdP. Link issuer ${issuer} and subject ${subject} to ${row.email || row.id}? Existing browser sessions for this user will be revoked.`)) return;
    button.disabled = true; clearError();
    try {
      await api(`/admin/api/users/${encodeURIComponent(row.id)}/identity`, { method: "POST", body: { issuer, subject, identity_verified: true } });
      message("Identity linked. API keys are unchanged. SSO still requires an authorized group.");
      await loadView();
    } catch (error) { showError(error); }
    finally { button.disabled = false; }
  }
  $("revoke-all-sessions").addEventListener("click", (event) => sessionAction(null, event.currentTarget));
  function renderEditor() {
    const resourceFields = fields[state.view];
    $("editor").hidden = !resourceFields;
    const form = $("resource-form"); form.replaceChildren();
    if (!resourceFields) return;
    $("editor-title").textContent = `Create ${state.view === "keys" ? "API key" : state.view.slice(0, -1)}`;
    resourceFields.forEach((field) => {
      const wrap = node("div", undefined, field.type === "json" ? "field-wide" : "");
      const id = `field-${field.name}`; const label = node("label", field.label); label.htmlFor = id;
      const input = node(field.options ? "select" : field.type === "json" ? "textarea" : "input");
      input.id = id; input.name = field.name; input.required = Boolean(field.required);
      if (field.options) field.options.forEach((option) => { const opt = node("option", option); opt.value = option; input.append(opt); });
      else if (field.type === "json") { input.rows = 5; input.spellcheck = false; }
      else input.type = field.type || "text";
      if (field.type === "checkbox") input.checked = Boolean(field.value);
      else input.value = field.value ?? "";
      if (field.min) input.min = field.min;
      if (field.max) input.max = field.max;
      if (field.step) input.step = field.step;
      if (field.type === "password") { input.autocomplete = "off"; input.spellcheck = false; }
      wrap.append(label, input);
      if (field.help) { const help = node("small", field.help); help.id = `${id}-help`; input.setAttribute("aria-describedby", help.id); wrap.append(help); }
      form.append(wrap);
    });
    const button = node("button", "Create"); button.type = "submit"; form.append(button);
  }
  async function loadView(rebuild = false) {
    const generation = ++state.generation;
    $("refresh").disabled = true;
    clearError();
    $("view-title").textContent = state.view[0].toUpperCase() + state.view.slice(1);
    $("view-description").textContent = descriptions[state.view];
    $("evaluation").hidden = state.view !== "evaluation";
    $("table-wrap").hidden = ["evaluation", "prompts"].includes(state.view);
    $("prompts").hidden = state.view !== "prompts";
    $("summary").hidden = state.view !== "overview";
    if (rebuild) renderEditor();
    try {
      if (state.view === "evaluation") return;
      if (state.view === "prompts") { await loadPrompts(); return; }
      const data = await api(`/admin/api/${state.view === "overview" ? "usage" : state.view}`);
      if (generation !== state.generation || !state.authenticated) return;
      renderTable(data);
      if (state.view === "overview") {
        const settings = await api("/admin/api/settings");
        if (generation !== state.generation || !state.authenticated) return;
        $("summary").replaceChildren(node("h2", "Runtime settings"), node("pre", safeValue(settings)));
      }
    } catch (error) { if (generation === state.generation) showError(error); }
    finally { if (generation === state.generation) $("refresh").disabled = false; }
  }
  const promptState = { templates: [], versions: [], generation: 0 };
  const promptBase = "/admin/api/prompt-registry";
  function promptPath() {
    if (!$("prompt-template").value || !$("prompt-version").value) throw new Error("Select a stored template and version first.");
    return `${promptBase}/templates/${encodeURIComponent($("prompt-template").value)}/versions/${encodeURIComponent($("prompt-version").value)}`;
  }
  function promptActive(generation) { return state.authenticated && state.view === "prompts" && generation === promptState.generation; }
  async function loadPrompts(selected) {
    const generation = ++promptState.generation;
    const previous = selected || $("prompt-template").value;
    const templates = await api(`${promptBase}/templates`);
    if (!promptActive(generation)) return;
    promptState.templates = templates;
    $("prompt-template").replaceChildren();
    templates.forEach((row) => { const option = node("option", `${row.name}${row.archived ? " (archived)" : ""}`); option.value = row.id; $("prompt-template").append(option); });
    if (templates.some((row) => row.id === previous)) $("prompt-template").value = previous;
    await loadPromptVersions();
  }
  async function loadPromptVersions(selectLatest = false) {
    const generation = ++promptState.generation;
    const templateId = $("prompt-template").value;
    const previous = $("prompt-version").value;
    const template = promptState.templates.find((row) => row.id === templateId);
    $("prompt-archive").disabled = !template;
    $("prompt-archive").textContent = template?.archived ? "Restore template" : "Archive template";
    $("prompt-version-form").querySelector("button").disabled = !template || template.archived;
    $("prompt-version").replaceChildren();
    $("prompt-version-content").textContent = ""; $("prompt-preview").textContent = ""; $("prompt-reports").textContent = "";
    promptState.versions = [];
    if (!templateId) return;
    const versions = await api(`${promptBase}/templates/${encodeURIComponent(templateId)}/versions`);
    if (!promptActive(generation)) return;
    promptState.versions = versions;
    versions.forEach((row) => { const option = node("option", `Version ${row.version}`); option.value = row.version; $("prompt-version").append(option); });
    if (selectLatest && versions.length) $("prompt-version").value = versions[versions.length - 1].version;
    else if (versions.some((row) => String(row.version) === previous)) $("prompt-version").value = previous;
    await showPromptVersion();
  }
  async function showPromptVersion() {
    const generation = ++promptState.generation;
    const version = promptState.versions.find((row) => String(row.version) === $("prompt-version").value);
    $("prompt-version-content").textContent = version?.content || "";
    $("prompt-preview").textContent = ""; $("prompt-reports").textContent = "";
    $("prompt-variables").value = JSON.stringify(Object.fromEntries((version?.variables || []).map((name) => [name, ""])), null, 2);
    if (!version) return;
    const reports = await api(`${promptBase}/evaluations?prompt_version_id=${encodeURIComponent(version.id)}`);
    if (promptActive(generation)) $("prompt-reports").textContent = reports.length ? JSON.stringify(reports, null, 2) : "No saved reports for this version.";
  }
  function promptForm(id, action) {
    $(id).addEventListener("submit", async (event) => {
      event.preventDefault(); clearError(); const button = event.submitter; button.disabled = true;
      try { await action(); } catch (error) { if (state.authenticated) showError(error); }
      finally { button.disabled = false; }
    });
  }
  promptForm("prompt-create-form", async () => {
    const result = await api(`${promptBase}/templates`, { method: "POST", body: { name: $("prompt-name").value, content: $("prompt-content").value } });
    if (!state.authenticated || state.view !== "prompts") return;
    $("prompt-create-form").reset(); message("Template and immutable version 1 stored."); await loadPrompts(result.template.id);
  });
  promptForm("prompt-version-form", async () => {
    if (!$("prompt-template").value) throw new Error("Select a template first.");
    const path = `${promptBase}/templates/${encodeURIComponent($("prompt-template").value)}/versions`;
    await api(path, { method: "POST", body: { content: $("prompt-next-content").value } });
    if (!state.authenticated || state.view !== "prompts") return;
    $("prompt-next-content").value = ""; message("Immutable version appended."); await loadPromptVersions(true);
  });
  promptForm("prompt-render-form", async () => {
    const generation = promptState.generation;
    const result = await api(`${promptPath()}/render`, { method: "POST", body: { variables: JSON.parse($("prompt-variables").value) } });
    if (promptActive(generation)) $("prompt-preview").textContent = result.content;
  });
  promptForm("prompt-evaluate-form", async () => {
    const generation = promptState.generation;
    await api(`${promptPath()}/evaluations`, { method: "POST", body: { cases: JSON.parse($("prompt-cases").value), rules: JSON.parse($("prompt-rules").value) } });
    if (promptActive(generation)) { message("Deterministic evaluation report saved."); await showPromptVersion(); }
  });
  $("prompt-template").addEventListener("change", () => loadPromptVersions().catch(showError));
  $("prompt-version").addEventListener("change", () => showPromptVersion().catch(showError));
  $("prompt-archive").addEventListener("click", async (event) => {
    const template = promptState.templates.find((row) => row.id === $("prompt-template").value);
    if (!template || !window.confirm(`${template.archived ? "Restore" : "Archive"} template ${template.name}? Existing versions and reports are retained.`)) return;
    const button = event.currentTarget; button.disabled = true;
    try { await api(`${promptBase}/templates/${encodeURIComponent(template.id)}`, { method: "PATCH", body: { archived: !template.archived } }); if (state.authenticated && state.view === "prompts") await loadPrompts(template.id); }
    catch (error) { if (state.authenticated) showError(error); }
    finally { button.disabled = false; }
  });
  function clearPrompts() {
    ++promptState.generation; promptState.templates = []; promptState.versions = [];
    ["prompt-name", "prompt-content", "prompt-next-content", "prompt-variables", "prompt-cases", "prompt-rules"].forEach((id) => { $(id).value = ""; });
    ["prompt-template", "prompt-version", "prompt-version-content", "prompt-preview", "prompt-reports"].forEach((id) => $(id).replaceChildren());
  }
  async function connect() {
    const session = await api("/auth/session");
    if (!session?.authenticated) throw new Error("Sign-in is required. Check the administrator credential or use SSO.");
    if (session.role !== "admin") throw new Error("Administrator access is required for this interface.");
    state.csrf = session.csrf_token || ""; state.authenticated = true;
    $("identity").textContent = `${session.email || "Administrator"} · ${session.role || "authenticated"}`;
    $("login").hidden = true; $("workspace").hidden = false;
    await loadView(true);
  }
  $("bootstrap-form").addEventListener("submit", async (event) => {
    event.preventDefault(); clearError(); const button = event.submitter; button.disabled = true;
    const token = $("bootstrap-token").value; $("bootstrap-token").value = "";
    try {
      const result = await api("/bootstrap", { method: "POST", body: { email: $("bootstrap-email").value.trim(), token } });
      showSecret(result); message("Initial administrator created. Save the key, then use it to connect above. Disable bootstrap access after setup.");
    } catch (error) { showError(error); }
    finally { button.disabled = false; }
  });
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault(); clearError();
    const button = event.submitter; button.disabled = true;
    state.bearer = $("admin-token").value.trim(); $("admin-token").value = "";
    try { await connect(); message("Connected. Credentials are held only in this page's memory."); }
    catch (error) { state.bearer = ""; showError(error); }
    finally { button.disabled = false; }
  });
  $("navigation").addEventListener("click", (event) => {
    const target = event.target.closest("button[data-view]"); if (!target) return;
    state.view = target.dataset.view;
    document.querySelectorAll("[data-view]").forEach((button) => button.removeAttribute("aria-current"));
    target.setAttribute("aria-current", "page"); loadView(true);
  });
  $("refresh").addEventListener("click", () => loadView());
  $("resource-form").addEventListener("submit", async (event) => {
    event.preventDefault(); clearError(); const view = state.view; const button = event.submitter; button.disabled = true;
    try {
      const payload = {};
      fields[view].forEach((field) => {
        const input = event.target.elements.namedItem(field.name);
        payload[field.name] = field.type === "checkbox" ? input.checked : field.type === "json" ? JSON.parse(input.value) : field.type === "number" ? Number(input.value) : input.value.trim();
      });
      if (payload.team_id === "") payload.team_id = null;
      const data = await api(`/admin/api/${view}`, { method: "POST", body: payload });
      // Clear provider secrets before any further asynchronous work.
      const secretInput = event.target.elements.namedItem("api_key"); if (secretInput) secretInput.value = "";
      if (!state.authenticated) return;
      showSecret(data); message(`Created ${view === "keys" ? "API key" : view.slice(0, -1)}.`);
      if (view === state.view) await loadView();
    } catch (error) { showError(error); }
    finally { button.disabled = false; }
  });
  $("evaluation-form").addEventListener("submit", async (event) => {
    event.preventDefault(); clearError(); const button = event.submitter; button.disabled = true; $("eval-result").textContent = "";
    try {
      const cases = JSON.parse($("eval-cases").value); const rules = JSON.parse($("eval-rules").value);
      if (!Array.isArray(cases) || !Array.isArray(rules)) throw new Error("Cases and rules must both be JSON arrays.");
      const result = await api("/admin/api/evaluate", { method: "POST", body: { cases, rules } });
      if (state.authenticated) $("eval-result").textContent = JSON.stringify(result, null, 2);
    } catch (error) { showError(error); }
    finally { button.disabled = false; }
  });
  $("disconnect").addEventListener("click", async () => {
    clearError();
    try { await api("/auth/logout", { method: "POST" }); }
    catch (error) { showError(new Error(`Server sign-out could not be confirmed: ${error.message}. Close this tab and end the session at your identity provider if necessary.`)); }
    finally {
      state.bearer = ""; state.csrf = ""; state.authenticated = false; ++state.generation; clearPrompts();
      $("secret-dialog").close(); clearSecret(); $("resource-form").replaceChildren(); $("table-wrap").replaceChildren(); $("summary").replaceChildren(); $("eval-result").textContent = "";
      $("workspace").hidden = true; $("login").hidden = false; $("identity").textContent = "Not signed in"; message("Local credentials cleared.");
    }
  });
  $("close-secret").addEventListener("click", () => $("secret-dialog").close());
  $("secret-dialog").addEventListener("close", clearSecret);
  $("secret-dialog").addEventListener("cancel", clearSecret);
  $("copy-secret").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("new-secret").value); $("copy-status").textContent = "Copied. Clear your clipboard after storing the credential."; }
    catch (_) { $("copy-status").textContent = "Clipboard unavailable. Select the credential and copy it manually."; }
  });
  window.addEventListener("pagehide", () => { state.bearer = ""; state.csrf = ""; clearSecret(); clearPrompts(); });
  connect().catch(() => { /* A signed-out page is the expected initial state. */ });
})();
