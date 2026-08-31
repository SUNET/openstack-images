/* SUNET Cloud Portal — vanilla JS SPA on the eduID design system */

const $ = (sel) => document.querySelector(sel);
const app = $("#app");
const nav = $("#nav");
const topbar = $("#topbar");
const userBlock = $("#user-block");
const signoutLink = $("#signout-link");

let currentUser = null;

// ---------- DOM helpers ----------

function h(tag, attrs = {}, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
        if (v == null || v === false) continue;
        if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
        else if (k === "className") el.className = v;
        else if (k === "htmlFor") el.setAttribute("for", v);
        else if (k === "dataset") for (const [dk, dv] of Object.entries(v)) el.dataset[dk] = dv;
        else if (v === true) el.setAttribute(k, "");
        else el.setAttribute(k, v);
    }
    for (const child of children.flat()) {
        if (child == null || child === false) continue;
        if (typeof child === "string" || typeof child === "number")
            el.appendChild(document.createTextNode(String(child)));
        else el.appendChild(child);
    }
    return el;
}

const clear = (el) => { el.innerHTML = ""; return el; };

function svgPlus() {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("width", "12"); svg.setAttribute("height", "12");
    svg.setAttribute("viewBox", "0 0 12 12"); svg.setAttribute("fill", "none");
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", "M6 2v8M2 6h8");
    p.setAttribute("stroke", "currentColor"); p.setAttribute("stroke-width", "2");
    p.setAttribute("stroke-linecap", "round");
    svg.appendChild(p);
    return svg;
}

function svgArrow() {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("width", "14"); svg.setAttribute("height", "14");
    svg.setAttribute("viewBox", "0 0 14 14"); svg.setAttribute("fill", "none");
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", "M3 7h8M7 3l4 4-4 4");
    p.setAttribute("stroke", "currentColor"); p.setAttribute("stroke-width", "2");
    p.setAttribute("stroke-linecap", "round"); p.setAttribute("stroke-linejoin", "round");
    svg.appendChild(p);
    return svg;
}

// ---------- Router ----------

function navigate(hash) {
    const target = hash.startsWith("#") ? hash : "#" + hash;
    if (location.hash === target) route();
    else location.hash = target;
}

function currentRoute() { return location.hash.replace(/^#\/?/, ""); }

async function route() {
    if (!currentUser) {
        try {
            currentUser = await api("/api/me");
            if (!currentUser) { renderLogin(); return; }
        } catch {
            renderLogin();
            return;
        }
    }
    renderShell();

    const path = currentRoute();
    const parts = path.split("/").filter(Boolean);

    // Admin routes
    if (parts[0] === "admin" && parts[1] === "pricing" && parts[2] === "docs")
        return renderPricingDocs();
    if (parts[0] === "admin" && parts[1] === "billing")
        return renderAdminBillingJobs();
    if (parts[0] === "admin" && parts[1] === "pricing")
        return renderAdminPricing();
    if (parts[0] === "admin" && parts[1] === "clusters" && parts[2] === "new")
        return renderAdminCreateCluster();
    if (parts[0] === "admin" && parts[1] === "clusters" && parts[2] === "help")
        return renderClusterSetupHelp();
    if (parts[0] === "admin" && parts[1] === "clusters" && parts[2])
        return renderAdminClusterDetail(decodeURIComponent(parts[2]));
    if (parts[0] === "admin" && parts[1] === "clusters")
        return renderAdminClusters();
    if (parts[0] === "admin" && parts[1] === "cluster-requests")
        return renderAdminClusterRequests();
    if (parts[0] === "admin" && parts[1] === "contracts" && parts[2] === "edit" && parts[3])
        return renderAdminEditContract(parts[3]);
    if (parts[0] === "admin" && parts[1] === "contracts" && parts[2])
        return renderAdminContractDetail(parts[2]);
    if (parts[0] === "admin" && parts[1] === "customers" && parts[2] === "edit" && parts[3])
        return renderAdminEditCustomer(parts[3]);
    if (parts[0] === "admin" && parts[1] === "customers" && parts[2])
        return renderAdminCustomerDetail(parts[2]);
    if (parts[0] === "admin" && parts[1] === "customers")
        return renderAdminCustomers();
    if (parts[0] === "admin")
        return renderAdmin();

    // Billing routes
    if (parts[0] === "billing" && parts[1] === "new")
        return renderCreateBillingJob();
    if (parts[0] === "billing" && parts[1] === "run-once")
        return renderRunOnce();
    if (parts[0] === "billing" && parts[1] && parts[2] === "edit")
        return renderEditBillingJob(parts[1]);
    if (parts[0] === "billing" && parts[1])
        return renderBillingJobDetail(parts[1]);
    if (parts[0] === "billing")
        return renderBillingJobs();

    // Cluster routes (member-facing)
    if (parts[0] === "clusters" && parts[1] && parts[2] === "users")
        return renderClusterUsers(decodeURIComponent(parts[1]));
    if (parts[0] === "clusters" && parts[1])
        return renderClusterDetail(decodeURIComponent(parts[1]));
    if (parts[0] === "clusters")
        return renderClusters();

    // Customer routes
    if (parts[0] === "contracts" && parts[2] === "projects" && parts[3] === "new")
        return renderCreateProject(decodeURIComponent(parts[1]));
    if (parts[0] === "contracts" && parts[2] === "projects" && parts[3] === "edit" && parts[4])
        return renderEditProject(decodeURIComponent(parts[1]), decodeURIComponent(parts[4]));
    if (parts[0] === "contracts" && parts[2] === "projects" && parts[3])
        return renderProjectDetail(decodeURIComponent(parts[1]), decodeURIComponent(parts[3]));
    if (parts[0] === "contracts" && parts[2] === "projects")
        return renderContractProjects(decodeURIComponent(parts[1]));

    return renderContracts();
}

window.addEventListener("hashchange", route);

document.addEventListener("DOMContentLoaded", () => {
    const toggle = document.getElementById("mobile-toggle");
    const topbar = document.getElementById("topbar");
    if (toggle && topbar) {
        toggle.addEventListener("click", () => topbar.classList.toggle("open"));
    }
});

// ---------- API ----------

/**
 * Render a FastAPI error `detail` into a readable string. `detail` is a
 * plain string for HTTPException, but an array of {loc, msg} objects for
 * 422 validation errors — which would otherwise stringify to "[object
 * Object]".
 */
function formatApiError(detail) {
    if (detail == null) return "";
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
        return detail.map((e) => {
            const loc = Array.isArray(e.loc)
                ? e.loc.filter((p) => p !== "body").join(".")
                : "";
            return loc ? `${loc}: ${e.msg}` : (e.msg || JSON.stringify(e));
        }).join("; ");
    }
    return JSON.stringify(detail);
}

async function api(path, opts = {}) {
    const resp = await fetch(path, {
        headers: { "Content-Type": "application/json", ...opts.headers },
        ...opts,
    });
    if (resp.status === 401) { currentUser = null; renderLogin(); return null; }
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        throw new Error(formatApiError(err.detail) || "Request failed");
    }
    if (resp.status === 204) return null;
    return resp.json();
}

// ---------- Shell ----------

function navKeyFromHash() {
    const parts = currentRoute().split("/").filter(Boolean);
    if (parts[0] === "admin") return "admin";
    if (parts[0] === "billing") return "billing";
    if (parts[0] === "clusters" || parts[0] === "contracts" || !parts[0]) return "contracts";
    return "";
}

function renderShell() {
    if (!currentUser) {
        topbar.hidden = true;
        return;
    }
    topbar.hidden = false;
    const isAdmin = !!currentUser.is_admin;
    const active = navKeyFromHash();

    clear(nav);
    const links = [
        { key: "contracts", label: "My Contracts", hash: "#/contracts" },
        { key: "billing", label: "Billing", hash: "#/billing" },
    ];
    if (isAdmin) links.push({ key: "admin", label: "Admin", hash: "#/admin" });
    for (const l of links) {
        nav.appendChild(h("a", {
            href: l.hash,
            className: l.key === active ? "on" : "",
            dataset: { key: l.key },
        }, l.label));
    }

    clear(userBlock);
    userBlock.hidden = false;
    const display = currentUser.name || currentUser.email || currentUser.sub;
    const initials = (display || "??").split(/[\s@.]+/).filter(Boolean).slice(0, 2).map(s => s[0]).join("") || "U";
    userBlock.appendChild(h("div", { className: "avatar" }, initials));
    userBlock.appendChild(h("div", {},
        h("div", {}, display),
        h("div", { className: "sub" }, isAdmin ? "Operator" : (currentUser.email || currentUser.sub)),
    ));
    signoutLink.hidden = false;
}

// ---------- Common building blocks ----------

function bc(...items) {
    const wrap = h("nav", { className: "bc" });
    items.forEach((item, i) => {
        if (i > 0) wrap.appendChild(h("span", { className: "sep" }, "/"));
        if (item.hash && i < items.length - 1)
            wrap.appendChild(h("a", { href: "#" + item.hash }, item.label));
        else
            wrap.appendChild(h("span", { className: "cur" }, item.label));
    });
    return wrap;
}

function phead({ eyebrow, title, lead, actions } = {}) {
    const wrap = h("div", { className: "phead" });
    const inner = h("div", actions ? { className: "phead-row" } : {});
    const titleBlock = h("div", {});
    if (eyebrow) titleBlock.appendChild(h("div", { className: "eyebrow" }, eyebrow));
    if (title) titleBlock.appendChild(h("h1", {}, title));
    if (lead) titleBlock.appendChild(h("p", { className: "lead" }, lead));
    inner.appendChild(titleBlock);
    if (actions) inner.appendChild(h("div", { className: "phead-actions" }, ...actions));
    wrap.appendChild(inner);
    return wrap;
}

function slbl(text, count, { help } = {}) {
    const wrap = h("div", { className: "slbl" });
    wrap.appendChild(document.createTextNode(text));
    if (count != null) wrap.appendChild(h("span", { className: "count" }, "· " + count));
    if (help) {
        const a = h("a", { className: "help", onclick: (e) => { e.preventDefault(); help.onClick && help.onClick(); } }, help.label);
        if (help.href) a.setAttribute("href", help.href);
        wrap.appendChild(a);
    }
    return wrap;
}

function kvRow(k, v) {
    return h("div", { className: "row" },
        h("div", { className: "k" }, k),
        v instanceof Node ? h("div", { className: "v" }, v) : h("div", { className: "v" }, v ?? "—"),
    );
}

function kvRowMono(k, v) {
    return h("div", { className: "row" },
        h("div", { className: "k" }, k),
        h("div", { className: "v mono" }, v ?? "—"),
    );
}

function kv(...rows) {
    return h("div", { className: "kv" }, ...rows);
}

function badge(text, kind) { return h("span", { className: "badge " + (kind || "neutral") }, text); }

function phaseBadge(phase) {
    if (!phase) return badge("Unknown", "pending");
    if (phase === "Ready") return badge("Ready", "ready");
    if (phase === "Provisioning" || phase === "Pending") return badge(phase, "pending");
    if (phase.toLowerCase().includes("error") || phase.toLowerCase().includes("failed")) return badge(phase, "error");
    return badge(phase, "pending");
}

function showAlert(msg, type = "error") {
    const existing = app.querySelector(".alert");
    if (existing) existing.remove();
    app.prepend(h("div", { className: `alert ${type}` }, msg));
    if (type === "success") setTimeout(() => app.querySelector(".alert.success")?.remove(), 4000);
}

function emptyState(text) {
    return h("div", { className: "empty" }, text);
}

function fmtDate(s) {
    if (!s) return "—";
    try {
        return new Date(s).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    } catch { return s; }
}

function fmtDay(s) {
    if (!s) return "—";
    try { return new Date(s).toLocaleDateString(undefined, { dateStyle: "medium" }); }
    catch { return s; }
}

// ---------- Login ----------

function renderLogin() {
    topbar.hidden = true;
    clear(app);
    app.className = "login-page";
    app.appendChild(h("div", { className: "login-card" },
        h("div", { className: "mark" }, "S"),
        h("h1", {}, "Sign in"),
        h("p", { className: "lead" }, "Use your federated identity to manage your contracts, projects and billing exports."),
        h("a", { href: "/auth/login", className: "btn primary" }, "Continue with SWAMID", svgArrow()),
        h("p", { className: "tag" }, "single sign-on · sunet"),
    ));
}

// ---------- Customer: Contracts list ----------

async function renderContracts() {
    clear(app);
    app.className = "page";
    app.appendChild(phead({
        eyebrow: "Customer",
        title: "Your contracts",
        lead: "Choose a contract to manage its cloud projects, members and billing.",
    }));

    const contracts = currentUser.contracts || [];
    if (!contracts.length) {
        app.appendChild(slbl("Active contracts", 0));
        app.appendChild(emptyState("You don't have access to any contracts yet. Ask an administrator to grant you access."));
        return;
    }

    app.appendChild(slbl("Active contracts", contracts.length));
    const grid = h("div", { className: "grid" });
    for (const c of contracts) {
        const cn = encodeURIComponent(c.contract_number);
        grid.appendChild(h("a", { className: "card link", href: `#/contracts/${cn}/projects` },
            h("div", { className: "card-head" },
                h("h3", {}, c.contract_number),
                badge(c.customer.domain, "active"),
            ),
            h("div", { className: "meta" }, c.customer.name + (c.description ? " — " + c.description : "")),
        ));
    }
    app.appendChild(grid);

    app.appendChild(h("p", { className: "hint", style: "margin-top:18px" },
        "Don't see a contract you expected? Ask your contract administrator to grant you access."));
}

// ---------- Customer: Projects in one contract ----------

async function renderContractProjects(contractNumber) {
    clear(app);
    app.className = "page";
    const contractInfo = currentUser.contracts.find(c => c.contract_number === contractNumber);
    const customerName = contractInfo ? contractInfo.customer.name : "";
    const cn = encodeURIComponent(contractNumber);

    app.appendChild(bc(
        { label: "Contracts", hash: "/contracts" },
        { label: contractNumber },
    ));
    app.appendChild(phead({
        eyebrow: "Contract",
        title: contractNumber,
        lead: customerName + (contractInfo?.description ? " — " + contractInfo.description : ""),
        actions: [
            h("a", { className: "btn primary", href: `#/contracts/${cn}/projects/new` }, svgPlus(), "New project"),
        ],
    }));

    try {
        const projects = await api(`/api/contracts/${contractNumber}/projects`);
        app.appendChild(slbl("Projects", projects.length));
        if (!projects.length) {
            app.appendChild(emptyState("No projects yet. Create one to get started."));
        } else {
            for (const p of projects) {
                const rn = encodeURIComponent(p.resource_name);
                const head = h("div", { className: "card-head" },
                    h("h3", {}, p.name),
                    phaseBadge(p.phase),
                );
                if (p.managed) head.appendChild(badge("managed-by-sunet", "managed"));
                app.appendChild(h("a", { className: "card link", href: `#/contracts/${cn}/projects/${rn}` },
                    head,
                    p.description ? h("div", { className: "meta" }, p.description) : null,
                    h("div", { className: "meta mono" },
                        (p.managed
                            ? "SUNET-managed"
                            : `${p.users.length} member${p.users.length === 1 ? "" : "s"}`)
                        + ` · ${p.resource_name}`),
                ));
            }
        }

        // Clusters belonging to this contract.
        const allClusters = await api("/api/clusters");
        const clusters = (allClusters || []).filter(c => c.contract_number === contractNumber);
        app.appendChild(slbl("Clusters", clusters.length));
        if (!clusters.length) {
            app.appendChild(emptyState("No clusters on this contract. SUNET ops provisions clusters — contact them to request one."));
        } else {
            for (const c of clusters) {
                const slug = encodeURIComponent(c.slug);
                app.appendChild(h("a", { className: "card link", href: `#/clusters/${slug}` },
                    h("div", { className: "card-head" },
                        h("h3", {}, c.name),
                        c.provisioned_at ? badge("provisioned", "ok") : badge("pending", "warn"),
                    ),
                    h("div", { className: "meta" }, `${c.size_label} — ${c.total_servers} servers (3 controllers + ${3 * c.worker_groups} workers)`),
                    h("div", { className: "meta mono" },
                        `Your role: ${c.caller_role || "?"}` +
                        (c.active_addons.length ? ` · Addons: ${c.active_addons.join(", ")}` : "")),
                ));
            }
        }
    } catch (e) { showAlert(e.message); }
}

// ---------- Customer: Project detail ----------

async function renderProjectDetail(contractNumber, resourceName) {
    clear(app);
    app.className = "page";
    const cn = encodeURIComponent(contractNumber);
    const rn = encodeURIComponent(resourceName);

    app.appendChild(bc(
        { label: "Contracts", hash: "/contracts" },
        { label: contractNumber, hash: `/contracts/${cn}/projects` },
        { label: resourceName },
    ));

    try {
        const p = await api(`/api/contracts/${contractNumber}/projects/${resourceName}`);
        const canMutate = !p.managed || (currentUser && currentUser.is_admin);
        const actions = [];
        if (canMutate) {
            actions.push(h("a", { className: "btn ghost sm", href: `#/contracts/${cn}/projects/edit/${rn}` }, "Edit"));
        }
        app.appendChild(phead({
            eyebrow: p.managed ? "Project · managed-by-sunet" : "Project",
            title: p.name,
            lead: p.description || null,
            actions: actions.length ? actions : null,
        }));

        if (p.managed && !canMutate) {
            const note = h("p", { className: "hint", style: "margin-bottom:16px" },
                "This project is SUNET-managed and read-only. Use the cluster page to request changes.");
            app.appendChild(note);
        }

        app.appendChild(h("div", { className: "slbl first" }, "Status"));
        app.appendChild(kv(
            kvRow("Phase", phaseBadge(p.phase)),
            kvRowMono("Resource name", p.resource_name),
            kvRow("Contract", h("a", { className: "link", href: `#/contracts/${cn}/projects` }, p.contract_number)),
            p.managed ? kvRow("Ownership", badge("managed-by-sunet", "managed")) : null,
        ));

        app.appendChild(slbl("Members", p.users.length));
        if (!p.users.length) {
            app.appendChild(emptyState(p.managed
                ? "SUNET-managed: customer admins of the linked cluster get Keystone reader on this project automatically."
                : "No members yet. Add one via Edit."));
        } else {
            const ul = h("ul", { className: "ilist" });
            for (const u of p.users) {
                ul.appendChild(h("li", {},
                    h("span", {}, u),
                    h("span", { className: "meta" }, p.managed ? "reader" : "member"),
                ));
            }
            app.appendChild(ul);
        }

        app.appendChild(slbl("Quotas"));
        app.appendChild(quotaKv(p.quotas));

        if (canMutate) {
            const dz = h("div", { className: "danger-zone" });
            dz.appendChild(h("div", { className: "dz-head" }, "Danger zone"));
            dz.appendChild(h("div", { className: "slbl" }, "Delete project"));
            dz.appendChild(h("p", { className: "hint" },
                "Permanently removes the OpenStack project and all its resources. To move it to another contract, use the contract's danger zone."));
            dz.appendChild(h("div", { className: "btn-row" },
                h("button", { className: "btn danger sm", onclick: async () => {
                    if (!confirm(`Delete project ${p.name}? This will remove the OpenStack project and all its resources.`)) return;
                    try {
                        await api(`/api/contracts/${contractNumber}/projects/${resourceName}`, { method: "DELETE" });
                        navigate(`/contracts/${cn}/projects`);
                    } catch (err) { showAlert(err.message); }
                }}, "Delete project"),
            ));
            app.appendChild(dz);
        }
    } catch (e) { showAlert(e.message); }
}

// ---------- Quotas (shared) ----------

// Each field: { k, label, factor }. `factor` is how many stored (CR) units
// one displayed unit is worth — RAM is stored as ramMB but shown in GB, so
// factor 1024 (display GB = ramMB / 1024). Default factor 1 (no conversion).
const QUOTA_GROUPS = [
    { key: "compute", label: "Compute", fields: [
        { k: "instances", label: "Instances" },
        { k: "cores", label: "vCPUs" },
        { k: "ramMB", label: "RAM (GB)", factor: 1024 },
    ] },
    { key: "storage", label: "Storage", fields: [
        { k: "volumes", label: "Volumes" },
        { k: "volumesGB", label: "Volume storage (GB)" },
        { k: "snapshots", label: "Snapshots" },
    ] },
    { key: "network", label: "Network", fields: [
        { k: "securityGroups", label: "Security groups" },
        { k: "securityGroupRules", label: "Security group rules" },
    ] },
];

// The OpenStack quota APIs validate values as signed int32 and reject
// anything larger, so a bigger value in the CR wedges the operator.
// Mirrored server-side in schemas.py (_QUOTA_MAX).
const QUOTA_MAX = 2147483647;

const _qstored = (quotas, gk, k) =>
    (quotas && quotas[gk] && quotas[gk][k] != null) ? quotas[gk][k] : 0;

/** Build an editable quota form pre-filled from `values` (a quotas dict). */
function quotaForm(values, { warnLowering = false } = {}) {
    const form = h("form", { className: "form", style: "margin-top:14px", id: "quota-form" },
        h("h3", {}, "Quotas"),
        h("p", { className: "hint" }, "Resource limits for this project. You can change these anytime."),
        warnLowering ? h("p", { className: "hint" },
            "Lowering a quota below what the project already uses won't remove "
            + "existing resources, but you won't be able to create new ones of "
            + "that type until usage drops below the new limit.") : null,
    );
    for (const g of QUOTA_GROUPS) {
        form.appendChild(h("div", { className: "slbl" }, g.label));
        const grid = h("div", { style: "display:grid;grid-template-columns:1fr 130px;gap:8px 14px;align-items:center" });
        for (const f of g.fields) {
            const factor = f.factor || 1;
            const display = _qstored(values, g.key, f.k) / factor;
            grid.appendChild(h("label", { htmlFor: `q-${g.key}-${f.k}`, style: "margin:0" }, f.label));
            grid.appendChild(h("input", { id: `q-${g.key}-${f.k}`, name: `${g.key}.${f.k}`, type: "number", min: "0", max: String(Math.floor(QUOTA_MAX / factor)), step: factor === 1 ? "1" : "any", value: String(display), className: "mono" }));
        }
        form.appendChild(grid);
    }
    return form;
}

/** Read a quotas dict back out of a form built by quotaForm() (display -> stored). */
function readQuotaForm(form) {
    const q = {};
    for (const g of QUOTA_GROUPS) {
        q[g.key] = {};
        for (const f of g.fields) {
            const factor = f.factor || 1;
            const n = parseFloat(form.querySelector(`[name="${g.key}.${f.k}"]`).value);
            const display = Number.isFinite(n) && n >= 0 ? n : 0;
            q[g.key][f.k] = Math.min(Math.round(display * factor), QUOTA_MAX);
        }
    }
    return q;
}

/** Render quotas read-only as a kv block. */
function quotaKv(quotas) {
    const rows = [];
    for (const g of QUOTA_GROUPS) {
        for (const f of g.fields) {
            const has = quotas && quotas[g.key] && quotas[g.key][f.k] != null;
            const display = has ? _qstored(quotas, g.key, f.k) / (f.factor || 1) : "—";
            rows.push(kvRow(`${g.label}: ${f.label}`, String(display)));
        }
    }
    return kv(...rows);
}

// ---------- Customer: Create project ----------

function renderCreateProject(contractNumber) {
    clear(app);
    app.className = "page narrow-form";
    const contractInfo = currentUser.contracts.find(c => c.contract_number === contractNumber);
    const cn = encodeURIComponent(contractNumber);

    app.appendChild(bc(
        { label: "Contracts", hash: "/contracts" },
        { label: contractNumber, hash: `/contracts/${cn}/projects` },
        { label: "New project" },
    ));
    app.appendChild(phead({
        eyebrow: "New project",
        title: "Create a project",
        lead: "Projects map to a Kubernetes namespace and a billing scope. Choose a name and grant initial members access.",
    }));

    const identity = h("form", { className: "form", id: "identity-form" },
        h("h3", {}, "Identity"),
        h("label", { htmlFor: "name" }, "Project name"),
        h("input", { id: "name", name: "name", type: "text", required: true, maxlength: "64", pattern: "[a-z0-9]([a-z0-9-]*[a-z0-9])?", placeholder: "my-project", className: "mono" }),
        h("p", { className: "hint" }, "Lowercase, digits and hyphens only — must start and end with a letter or digit. Max 64 characters. Cannot be changed later."),
        h("label", { htmlFor: "desc" }, "Description"),
        h("textarea", { id: "desc", name: "description", className: "sans", placeholder: "What is this project for?" }),
        h("label", { htmlFor: "contract" }, "Contract"),
        (() => {
            const sel = h("select", { id: "contract", disabled: true });
            sel.appendChild(h("option", {}, contractNumber + (contractInfo ? " — " + contractInfo.customer.name : "")));
            return sel;
        })(),
        h("p", { className: "hint" }, "Set from the contract you came from."),
    );

    const access = h("form", { className: "form", style: "margin-top:14px", id: "access-form" },
        h("h3", {}, "Access"),
        h("label", { htmlFor: "members" }, "Members (one per line)"),
        h("textarea", { id: "members", name: "users", style: "min-height:140px", placeholder: "user1@idp\nuser2@idp" }),
        h("p", { className: "hint" }, "Add the SWAMID identifiers of users who can manage the project. You can update this anytime."),
    );

    const quota = quotaForm(currentUser.quota_defaults);

    const actions = h("div", { className: "btn-row" },
        h("button", { className: "btn primary", onclick: async (e) => {
            e.preventDefault();
            const name = identity.querySelector('[name="name"]').value.trim();
            const description = identity.querySelector('[name="description"]').value.trim();
            const usersRaw = access.querySelector('[name="users"]').value.trim();
            const users = usersRaw ? usersRaw.split("\n").map(u => u.trim()).filter(Boolean) : [];
            if (!quota.reportValidity()) return;
            const quotas = readQuotaForm(quota);
            try {
                await api(`/api/contracts/${contractNumber}/projects`, {
                    method: "POST", body: JSON.stringify({ name, description, users, quotas }),
                });
                navigate(`/contracts/${cn}/projects`);
            } catch (err) { showAlert(err.message); }
        }}, "Create project"),
        h("a", { className: "btn ghost", href: `#/contracts/${cn}/projects` }, "Cancel"),
    );

    app.appendChild(identity);
    app.appendChild(access);
    app.appendChild(quota);
    app.appendChild(actions);
}

// ---------- Customer: Edit project ----------

async function renderEditProject(contractNumber, resourceName) {
    clear(app);
    app.className = "page narrow-form";
    const cn = encodeURIComponent(contractNumber);
    const rn = encodeURIComponent(resourceName);

    app.appendChild(bc(
        { label: "Contracts", hash: "/contracts" },
        { label: contractNumber, hash: `/contracts/${cn}/projects` },
        { label: resourceName, hash: `/contracts/${cn}/projects/${rn}` },
        { label: "Edit" },
    ));

    try {
        const p = await api(`/api/contracts/${contractNumber}/projects/${resourceName}`);
        app.appendChild(phead({ eyebrow: "Edit project", title: p.name }));

        const identity = h("form", { className: "form" },
            h("h3", {}, "Identity"),
            h("label", {}, "Project name"),
            h("input", { value: p.name, disabled: true, className: "mono" }),
            h("label", { htmlFor: "desc" }, "Description"),
            (() => {
                const t = h("textarea", { id: "desc", name: "description", className: "sans" });
                t.value = p.description || "";
                return t;
            })(),
        );
        const access = h("form", { className: "form", style: "margin-top:14px" },
            h("h3", {}, "Access"),
            h("label", { htmlFor: "members" }, "Members (one per line)"),
            (() => {
                const t = h("textarea", { id: "members", name: "users", style: "min-height:140px" });
                t.value = (p.users || []).join("\n");
                return t;
            })(),
        );
        const quota = quotaForm(p.quotas, { warnLowering: true });
        const actions = h("div", { className: "btn-row" },
            h("button", { className: "btn primary", onclick: async (e) => {
                e.preventDefault();
                const description = identity.querySelector('[name="description"]').value.trim();
                const usersRaw = access.querySelector('[name="users"]').value.trim();
                const users = usersRaw ? usersRaw.split("\n").map(u => u.trim()).filter(Boolean) : [];
                if (!quota.reportValidity()) return;
                const quotas = readQuotaForm(quota);
                try {
                    await api(`/api/contracts/${contractNumber}/projects/${resourceName}`, {
                        method: "PATCH", body: JSON.stringify({ description, users, quotas }),
                    });
                    navigate(`/contracts/${cn}/projects/${rn}`);
                } catch (err) { showAlert(err.message); }
            }}, "Save changes"),
            h("a", { className: "btn ghost", href: `#/contracts/${cn}/projects/${rn}` }, "Cancel"),
        );

        app.appendChild(identity);
        app.appendChild(access);
        app.appendChild(quota);
        app.appendChild(actions);
    } catch (e) { showAlert(e.message); }
}

// ---------- Billing: list ----------

async function renderBillingJobs() {
    clear(app);
    app.className = "page";
    app.appendChild(phead({
        eyebrow: "Billing",
        title: "Billing export jobs",
        lead: "Scheduled exports that deliver monthly billing CSVs to your finance system. Each job covers one or more contracts.",
        actions: [
            h("a", { className: "btn ghost", href: "#/billing/run-once" }, "Run once"),
            h("a", { className: "btn primary", href: "#/billing/new" }, svgPlus(), "New job"),
        ],
    }));

    try {
        const jobs = await api("/api/billing/jobs");
        const active = jobs.filter(j => j.enabled);
        const disabled = jobs.filter(j => !j.enabled);

        app.appendChild(slbl("Active jobs", active.length));
        if (!active.length) app.appendChild(emptyState("No active billing jobs."));
        for (const j of active) app.appendChild(billingJobCard(j));

        if (disabled.length) {
            app.appendChild(slbl("Disabled", disabled.length));
            for (const j of disabled) app.appendChild(billingJobCard(j));
        }
    } catch (e) { showAlert(e.message); }
}

function billingJobCard(j) {
    const scope = j.all_contracts ? "All your contracts" : `${j.contract_ids.length} contract${j.contract_ids.length === 1 ? "" : "s"}`;
    const target = j.delivery_method === "webdav"
        ? (j.delivery_config?.url ? `WebDAV → ${j.delivery_config.url}` : "WebDAV")
        : (j.delivery_config?.recipient ? `email → ${j.delivery_config.recipient}` : "email");
    return h("a", { className: "card link", href: `#/billing/${j.id}` },
        h("div", { className: "card-head" },
            h("h3", {}, j.name),
            j.enabled ? badge("Enabled", "ready") : badge("Disabled", "neutral"),
        ),
        h("div", { className: "meta" }, `${scope} · ${j.per_contract ? "per-contract files" : "single file"} · ${target}`),
        h("div", { className: "meta mono" }, j.schedule),
    );
}

// ---------- Billing: detail ----------

async function renderBillingJobDetail(jobId) {
    clear(app);
    app.className = "page";
    try {
        const job = await api(`/api/billing/jobs/${jobId}`);
        app.appendChild(bc(
            { label: "Billing jobs", hash: "/billing" },
            { label: job.name },
        ));
        app.appendChild(phead({
            eyebrow: "Billing job",
            title: job.name,
            lead: `Runs on schedule ${job.schedule} and delivers via ${job.delivery_method}.`,
            actions: [
                h("button", { className: "btn ghost sm", onclick: async () => {
                    try {
                        const r = await api(`/api/billing/jobs/${jobId}/run`, { method: "POST", body: JSON.stringify({}) });
                        showAlert(`Run completed: ${r.status}${r.files_delivered ? " · " + r.files_delivered + " files" : ""}`, r.status === "success" ? "success" : "error");
                        renderBillingJobDetail(jobId);
                    } catch (err) { showAlert(err.message); }
                }}, "Run now"),
                h("a", { className: "btn ghost sm", href: `#/billing/${jobId}/edit` }, "Edit"),
                h("button", { className: "btn danger sm", onclick: async () => {
                    if (!confirm(`Delete billing job "${job.name}"?`)) return;
                    try { await api(`/api/billing/jobs/${jobId}`, { method: "DELETE" }); navigate("/billing"); }
                    catch (err) { showAlert(err.message); }
                }}, "Delete"),
            ],
        }));

        const deliveryV = job.delivery_method === "webdav"
            ? h("span", {}, "WebDAV → ", h("span", { className: "mono" }, job.delivery_config?.url || "—"))
            : h("span", {}, "Email → ", h("span", { className: "mono" }, job.delivery_config?.recipient || "—"));

        app.appendChild(h("div", { className: "slbl first" }, "Configuration"));
        app.appendChild(kv(
            kvRow("Status", job.enabled ? badge("Enabled", "ready") : badge("Disabled", "neutral")),
            kvRowMono("Schedule", job.schedule),
            kvRow("Scope", job.all_contracts
                ? "All your accessible contracts"
                : `${job.contract_ids.length} contract${job.contract_ids.length === 1 ? "" : "s"} selected`),
            kvRow("Per-contract", job.per_contract ? "Yes — one file per contract per period" : "No — single file per period"),
            kvRow("Delivery", deliveryV),
            kvRowMono("Filename template", job.filename_template),
            kvRow("Owner", job.owner_sub),
        ));

        // Recent runs
        app.appendChild(h("div", { className: "slbl" }, "Recent runs"));
        const runs = await api(`/api/billing/jobs/${jobId}/runs`);
        if (!runs.length) {
            app.appendChild(emptyState("No executions yet."));
        } else {
            for (const r of runs) {
                const period = `${r.billing_period_start.substring(0, 7)} · ${r.files_delivered || 0} files delivered`;
                const row = h("div", { className: "run-row" },
                    h("div", { className: "when" }, fmtDay(r.started_at)),
                    h("div", { className: "det" }, period),
                    r.status === "success"
                        ? badge("Success", "success")
                        : r.status === "error" ? badge("Fail", "fail") : badge(r.status, "pending"),
                );
                if (r.error_message) row.appendChild(h("div", { className: "err" }, r.error_message));
                app.appendChild(row);
            }
        }

        // Manual run
        app.appendChild(h("div", { className: "slbl" }, "Manual run"));
        const now = new Date();
        const manualForm = h("div", { className: "form" },
            h("div", { className: "row-2" },
                h("div", { className: "field" },
                    h("label", { htmlFor: "year" }, "Year"),
                    h("input", { id: "year", name: "year", type: "number", value: String(now.getUTCFullYear()) }),
                ),
                h("div", { className: "field" },
                    h("label", { htmlFor: "month" }, "Month"),
                    h("input", { id: "month", name: "month", type: "number", min: "1", max: "12", value: String(now.getUTCMonth() + 1) }),
                ),
            ),
            h("div", { className: "btn-row" },
                h("button", { className: "btn primary sm", onclick: async () => {
                    const y = parseInt(manualForm.querySelector('[name="year"]').value, 10);
                    const m = parseInt(manualForm.querySelector('[name="month"]').value, 10);
                    try {
                        const r = await api(`/api/billing/jobs/${jobId}/run`, { method: "POST", body: JSON.stringify({ year: y, month: m }) });
                        showAlert(`Run completed: ${r.status}${r.files_delivered ? " · " + r.files_delivered + " files" : ""}`, r.status === "success" ? "success" : "error");
                        renderBillingJobDetail(jobId);
                    } catch (err) { showAlert(err.message); }
                }}, "Run for this period"),
                h("p", { className: "hint", style: "margin:0;align-self:center" }, "Re-runs are idempotent — same period overwrites the previous file."),
            ),
        );
        app.appendChild(manualForm);
    } catch (e) { showAlert(e.message); }
}

// ---------- Billing: create / edit ----------

function billingJobForm(job = null, onSubmit) {
    const contracts = currentUser.contracts || [];
    const isEdit = !!job;

    const basics = h("form", { className: "form" },
        h("h3", {}, "Basics"),
        h("label", { htmlFor: "name" }, "Job name"),
        h("input", { id: "name", name: "name", type: "text", required: true, value: job?.name || "" }),
        h("label", { className: "checkbox" },
            h("input", { type: "checkbox", name: "enabled", checked: job ? job.enabled : true }),
            "Enabled",
        ),
    );

    const contractSelect = h("select", { name: "contract_ids", multiple: true, size: String(Math.max(3, Math.min(8, contracts.length))) },
        ...contracts.map(c => {
            const o = h("option", { value: String(c.id) }, `${c.contract_number} — ${c.customer.name}`);
            if (job && job.contract_ids?.includes(c.id)) o.setAttribute("selected", "");
            return o;
        }),
    );
    const scope = h("form", { className: "form", style: "margin-top:14px" },
        h("h3", {}, "Scope"),
        h("label", { className: "checkbox" },
            h("input", { type: "checkbox", name: "all_contracts", checked: job ? job.all_contracts : true }),
            "Include all contracts you have access to",
        ),
        h("label", {}, "Or select specific contracts"),
        contractSelect,
        h("label", { className: "checkbox", style: "margin-top:18px" },
            h("input", { type: "checkbox", name: "per_contract", checked: job ? job.per_contract : false }),
            "Generate one file per contract",
        ),
        h("p", { className: "hint" }, "When unchecked, a single CSV containing all selected contracts is produced."),
    );

    const schedule = h("form", { className: "form", style: "margin-top:14px" },
        h("h3", {}, "Schedule"),
        h("label", { htmlFor: "cron" }, "Cron expression"),
        h("input", { id: "cron", name: "schedule", type: "text", className: "mono", required: true, value: job?.schedule || "0 6 1 * *", placeholder: "0 6 1 * *" }),
        h("p", { className: "hint" }, "Five-field cron syntax in UTC. Example: 0 6 1 * * runs at 06:00 on the first of every month."),
    );

    const dm = job?.delivery_method || "webdav";
    const dc = job?.delivery_config || {};
    const webdavWrap = h("div", { id: "webdav-config", style: dm === "webdav" ? "" : "display:none" },
        h("label", { htmlFor: "wurl" }, "WebDAV URL"),
        h("input", { id: "wurl", name: "webdav_url", type: "url", className: "mono", value: dc.url || "", placeholder: "https://finance.example.se/webdav/billing/" }),
        h("div", { className: "row-2" },
            h("div", { className: "field" },
                h("label", { htmlFor: "wuser" }, "Username"),
                h("input", { id: "wuser", name: "webdav_username", type: "text", value: dc.username || "" }),
            ),
            h("div", { className: "field" },
                h("label", { htmlFor: "wpw" }, "Password"),
                h("input", { id: "wpw", name: "webdav_password", type: "password", placeholder: isEdit ? "Leave blank to keep current" : "" }),
            ),
        ),
    );
    const emailWrap = h("div", { id: "email-config", style: dm === "email" ? "" : "display:none" },
        h("label", { htmlFor: "rcpt" }, "Recipient"),
        h("input", { id: "rcpt", name: "email_recipient", type: "email", value: dc.recipient || "", placeholder: "billing@example.se" }),
    );

    const dmSelect = h("select", { id: "dm", name: "delivery_method", onchange: (e) => {
        webdavWrap.style.display = e.target.value === "webdav" ? "block" : "none";
        emailWrap.style.display = e.target.value === "email" ? "block" : "none";
    }});
    const oWeb = h("option", { value: "webdav" }, "WebDAV");
    const oMail = h("option", { value: "email" }, "Email");
    if (dm === "webdav") oWeb.setAttribute("selected", "");
    if (dm === "email") oMail.setAttribute("selected", "");
    dmSelect.appendChild(oWeb);
    dmSelect.appendChild(oMail);

    const delivery = h("form", { className: "form", style: "margin-top:14px" },
        h("h3", {}, "Delivery"),
        h("label", { htmlFor: "dm" }, "Method"),
        dmSelect,
        webdavWrap,
        emailWrap,
        h("label", { htmlFor: "tpl" }, "Filename template"),
        h("input", { id: "tpl", name: "filename_template", type: "text", className: "mono", value: job?.filename_template || "billing-{year}-{month}.csv" }),
        h("p", { className: "hint" }, "Variables: ",
            h("code", {}, "{contract}"), " ",
            h("code", {}, "{year}"), " ",
            h("code", {}, "{month}"), " ",
            h("code", {}, "{day}"), " ",
            h("code", {}, "{date}")),
    );

    const submitBtn = h("button", { className: "btn primary", onclick: async (e) => {
        e.preventDefault();
        const name = basics.querySelector('[name="name"]').value.trim();
        const enabled = basics.querySelector('[name="enabled"]').checked;
        const allContracts = scope.querySelector('[name="all_contracts"]').checked;
        const perContract = scope.querySelector('[name="per_contract"]').checked;
        const contractIds = Array.from(contractSelect.selectedOptions).map(o => parseInt(o.value, 10));
        const scheduleVal = schedule.querySelector('[name="schedule"]').value.trim();
        const deliveryMethod = dmSelect.value;
        const filenameTemplate = delivery.querySelector('[name="filename_template"]').value.trim();
        const deliveryConfig = {};
        if (deliveryMethod === "webdav") {
            deliveryConfig.url = delivery.querySelector('[name="webdav_url"]').value.trim();
            deliveryConfig.username = delivery.querySelector('[name="webdav_username"]').value.trim();
            const pw = delivery.querySelector('[name="webdav_password"]').value;
            if (pw) deliveryConfig.password = pw;
            else if (isEdit) deliveryConfig.password = "********";
        } else {
            deliveryConfig.recipient = delivery.querySelector('[name="email_recipient"]').value.trim();
        }
        try {
            await onSubmit({
                name, enabled,
                all_contracts: allContracts,
                contract_ids: allContracts ? [] : contractIds,
                schedule: scheduleVal,
                delivery_method: deliveryMethod,
                delivery_config: deliveryConfig,
                filename_template: filenameTemplate,
                per_contract: perContract,
            });
        } catch (err) { showAlert(err.message); }
    }}, isEdit ? "Save changes" : "Create job");

    const cancel = h("a", { className: "btn ghost", href: isEdit ? `#/billing/${job.id}` : "#/billing" }, "Cancel");

    return [basics, scope, schedule, delivery, h("div", { className: "btn-row" }, submitBtn, cancel)];
}

function renderCreateBillingJob() {
    clear(app);
    app.className = "page narrow";
    app.appendChild(bc({ label: "Billing jobs", hash: "/billing" }, { label: "New job" }));
    app.appendChild(phead({
        eyebrow: "New billing job",
        title: "Configure a new export",
        lead: "Choose the schedule, scope and delivery destination for this billing export.",
    }));
    for (const node of billingJobForm(null, async (body) => {
        await api("/api/billing/jobs", { method: "POST", body: JSON.stringify(body) });
        navigate("/billing");
    })) app.appendChild(node);
}

async function renderEditBillingJob(jobId) {
    clear(app);
    app.className = "page narrow";
    try {
        const job = await api(`/api/billing/jobs/${jobId}`);
        app.appendChild(bc(
            { label: "Billing jobs", hash: "/billing" },
            { label: job.name, hash: `/billing/${jobId}` },
            { label: "Edit" },
        ));
        app.appendChild(phead({
            eyebrow: "Edit billing job",
            title: job.name,
            lead: "Configure when this job runs, which contracts it covers, and where the CSV is delivered.",
        }));
        for (const node of billingJobForm(job, async (body) => {
            await api(`/api/billing/jobs/${jobId}`, { method: "PATCH", body: JSON.stringify(body) });
            navigate(`/billing/${jobId}`);
        })) app.appendChild(node);
    } catch (e) { showAlert(e.message); }
}

// ---------- Billing: run once (ad-hoc, no saved job) ----------

function renderRunOnce() {
    clear(app);
    app.className = "page narrow";
    app.appendChild(bc({ label: "Billing jobs", hash: "/billing" }, { label: "Run once" }));
    app.appendChild(phead({
        eyebrow: "Run once",
        title: "Ad-hoc billing export",
        lead: "Generate and deliver a billing CSV a single time, without saving a recurring job.",
    }));

    const contracts = currentUser.contracts || [];

    const contractSelect = h("select", { name: "contract_ids", multiple: true, size: String(Math.max(3, Math.min(8, contracts.length))) },
        ...contracts.map(c => h("option", { value: String(c.id) }, `${c.contract_number} — ${c.customer.name}`)),
    );
    const scope = h("form", { className: "form" },
        h("h3", {}, "Scope"),
        h("label", { className: "checkbox" },
            h("input", { type: "checkbox", name: "all_contracts", checked: true }),
            "Include all contracts you have access to",
        ),
        h("label", {}, "Or select specific contracts"),
        contractSelect,
        h("label", { className: "checkbox", style: "margin-top:18px" },
            h("input", { type: "checkbox", name: "per_contract" }),
            "Generate one file per contract",
        ),
        h("p", { className: "hint" }, "When unchecked, a single CSV containing all selected contracts is produced."),
    );

    const period = h("form", { className: "form", style: "margin-top:14px" },
        h("h3", {}, "Period"),
        h("div", { className: "row-2" },
            h("div", { className: "field" },
                h("label", { htmlFor: "ro-year" }, "Year"),
                h("input", { id: "ro-year", name: "year", type: "number", className: "mono", placeholder: "previous month" }),
            ),
            h("div", { className: "field" },
                h("label", { htmlFor: "ro-month" }, "Month"),
                h("input", { id: "ro-month", name: "month", type: "number", min: "1", max: "12", className: "mono", placeholder: "previous month" }),
            ),
        ),
        h("p", { className: "hint" }, "Leave blank to bill the previous calendar month."),
    );

    const webdavWrap = h("div", { id: "webdav-config" },
        h("label", { htmlFor: "wurl" }, "WebDAV URL"),
        h("input", { id: "wurl", name: "webdav_url", type: "url", className: "mono", placeholder: "https://finance.example.se/webdav/billing/" }),
        h("div", { className: "row-2" },
            h("div", { className: "field" },
                h("label", { htmlFor: "wuser" }, "Username"),
                h("input", { id: "wuser", name: "webdav_username", type: "text" }),
            ),
            h("div", { className: "field" },
                h("label", { htmlFor: "wpw" }, "Password"),
                h("input", { id: "wpw", name: "webdav_password", type: "password" }),
            ),
        ),
    );
    const emailWrap = h("div", { id: "email-config", style: "display:none" },
        h("label", { htmlFor: "rcpt" }, "Recipient"),
        h("input", { id: "rcpt", name: "email_recipient", type: "email", placeholder: "billing@example.se" }),
    );

    const dmSelect = h("select", { id: "dm", name: "delivery_method", onchange: (e) => {
        webdavWrap.style.display = e.target.value === "webdav" ? "block" : "none";
        emailWrap.style.display = e.target.value === "email" ? "block" : "none";
    }},
        h("option", { value: "webdav" }, "WebDAV"),
        h("option", { value: "email" }, "Email"),
    );

    const delivery = h("form", { className: "form", style: "margin-top:14px" },
        h("h3", {}, "Delivery"),
        h("label", { htmlFor: "dm" }, "Method"),
        dmSelect,
        webdavWrap,
        emailWrap,
        h("label", { htmlFor: "tpl" }, "Filename template"),
        h("input", { id: "tpl", name: "filename_template", type: "text", className: "mono", value: "billing-{year}-{month}.csv" }),
        h("p", { className: "hint" }, "Variables: ",
            h("code", {}, "{contract}"), " ",
            h("code", {}, "{year}"), " ",
            h("code", {}, "{month}"), " ",
            h("code", {}, "{day}"), " ",
            h("code", {}, "{date}")),
    );

    const result = h("div", {});

    const submitBtn = h("button", { className: "btn primary", onclick: async (e) => {
        e.preventDefault();
        const allContracts = scope.querySelector('[name="all_contracts"]').checked;
        const perContract = scope.querySelector('[name="per_contract"]').checked;
        const contractIds = Array.from(contractSelect.selectedOptions).map(o => parseInt(o.value, 10));
        const deliveryMethod = dmSelect.value;
        const filenameTemplate = delivery.querySelector('[name="filename_template"]').value.trim();
        const yearVal = parseInt(period.querySelector('[name="year"]').value, 10);
        const monthVal = parseInt(period.querySelector('[name="month"]').value, 10);
        const deliveryConfig = {};
        if (deliveryMethod === "webdav") {
            deliveryConfig.url = delivery.querySelector('[name="webdav_url"]').value.trim();
            deliveryConfig.username = delivery.querySelector('[name="webdav_username"]').value.trim();
            const pw = delivery.querySelector('[name="webdav_password"]').value;
            if (pw) deliveryConfig.password = pw;
        } else {
            deliveryConfig.recipient = delivery.querySelector('[name="email_recipient"]').value.trim();
        }
        const body = {
            all_contracts: allContracts,
            contract_ids: allContracts ? [] : contractIds,
            delivery_method: deliveryMethod,
            delivery_config: deliveryConfig,
            filename_template: filenameTemplate,
            per_contract: perContract,
            year: Number.isNaN(yearVal) ? null : yearVal,
            month: Number.isNaN(monthVal) ? null : monthVal,
        };
        submitBtn.disabled = true;
        clear(result);
        try {
            const r = await api("/api/billing/run-once", { method: "POST", body: JSON.stringify(body) });
            const periodLabel = r.billing_period_start.substring(0, 7);
            if (r.status === "success") {
                result.appendChild(h("p", { className: "ok" }, `Delivered ${r.files_delivered} file(s) for ${periodLabel}.`));
            } else {
                result.appendChild(h("p", { className: "err" }, `Run failed: ${r.error_message || "unknown error"}`));
            }
        } catch (err) { showAlert(err.message); }
        finally { submitBtn.disabled = false; }
    }}, "Generate & deliver now");

    const cancel = h("a", { className: "btn ghost", href: "#/billing" }, "Cancel");

    app.appendChild(scope);
    app.appendChild(period);
    app.appendChild(delivery);
    app.appendChild(h("div", { className: "btn-row" }, submitBtn, cancel));
    app.appendChild(result);
}

// ---------- Admin: customers ----------

async function renderAdminCustomers() {
    clear(app);
    app.className = "page";
    app.appendChild(phead({
        eyebrow: "Admin",
        title: "Customers",
        lead: "Organisations with at least one active SUNET Cloud contract.",
        actions: [h("a", { className: "btn primary", href: "#/admin/customers/edit/new" }, svgPlus(), "New customer")],
    }));

    try {
        const customers = await api("/api/admin/customers");
        app.appendChild(slbl("All customers", customers.length));
        if (!customers.length) {
            app.appendChild(emptyState("No customers yet."));
            return;
        }
        for (const c of customers) {
            app.appendChild(h("a", { className: "card link", href: `#/admin/customers/${c.id}` },
                h("div", { className: "card-head" },
                    h("h3", {}, c.name),
                    badge(c.domain, "active"),
                ),
                c.description ? h("div", { className: "meta" }, c.description) : null,
                h("div", { className: "meta mono" }, `since ${fmtDay(c.created_at)}`),
            ));
        }
    } catch (e) { showAlert(e.message); }
}

async function renderAdminCustomerDetail(customerId) {
    clear(app);
    app.className = "page";

    try {
        const customer = await api(`/api/admin/customers/${customerId}`);
        app.appendChild(bc(
            { label: "Customers", hash: "/admin" },
            { label: customer.name },
        ));
        app.appendChild(phead({
            eyebrow: "Customer",
            title: customer.name,
            lead: `${customer.contracts.length} contract${customer.contracts.length === 1 ? "" : "s"} · onboarded ${fmtDay(customer.created_at)}.`,
            actions: [
                h("a", { className: "btn ghost sm", href: `#/admin/customers/edit/${customerId}` }, "Edit"),
                h("button", { className: "btn danger sm", onclick: async () => {
                    if (!confirm(`Delete customer ${customer.name}? All contracts must be deleted first.`)) return;
                    try { await api(`/api/admin/customers/${customerId}`, { method: "DELETE" }); navigate("/admin"); }
                    catch (err) { showAlert(err.message); }
                }}, "Delete"),
            ],
        }));

        app.appendChild(h("div", { className: "slbl first" }, "Identity"));
        app.appendChild(kv(
            kvRow("Name", customer.name),
            kvRowMono("Domain", customer.domain),
            kvRow("Description", customer.description || "—"),
            kvRow("Created", fmtDate(customer.created_at)),
        ));

        // Add contract form
        app.appendChild(h("div", { className: "slbl" }, "Add contract"));
        const addForm = h("form", { className: "form", onsubmit: async (e) => {
            e.preventDefault();
            const cn = addForm.querySelector('[name="contract_number"]').value.trim();
            const desc = addForm.querySelector('[name="description"]').value.trim();
            try {
                await api("/api/admin/contracts", { method: "POST", body: JSON.stringify({ customer_id: parseInt(customerId, 10), contract_number: cn, description: desc }) });
                renderAdminCustomerDetail(customerId);
            } catch (err) { showAlert(err.message); }
        }},
            h("div", { className: "row-2" },
                h("div", { className: "field" },
                    h("label", { htmlFor: "cn" }, "Contract number"),
                    h("input", { id: "cn", name: "contract_number", type: "text", required: true, pattern: "[A-Za-z0-9-]+", placeholder: "SUNET-2024-EXAMPLE-01" }),
                ),
                h("div", { className: "field" },
                    h("label", { htmlFor: "cd" }, "Description"),
                    h("input", { id: "cd", name: "description", type: "text", placeholder: "Optional" }),
                ),
            ),
            h("div", { className: "btn-row" },
                h("button", { type: "submit", className: "btn primary sm" }, "Add contract"),
            ),
        );
        app.appendChild(addForm);

        app.appendChild(slbl("Contracts", customer.contracts.length));
        if (!customer.contracts.length) {
            app.appendChild(emptyState("No contracts yet."));
        } else {
            for (const c of customer.contracts) {
                app.appendChild(h("a", { className: "card link", href: `#/admin/contracts/${c.id}` },
                    h("div", { className: "card-head" },
                        h("h3", {}, c.contract_number),
                        badge(c.description || "active", "active"),
                    ),
                    c.description ? h("div", { className: "meta" }, c.description) : null,
                    h("div", { className: "meta mono" }, `created ${fmtDay(c.created_at)}`),
                ));
            }
        }
    } catch (e) { showAlert(e.message); }
}

async function renderAdminEditCustomer(customerId) {
    clear(app);
    app.className = "page narrow-form";
    const isNew = customerId === "new";

    let customer = { name: "", domain: "", description: "" };
    if (!isNew) {
        try { customer = await api(`/api/admin/customers/${customerId}`); }
        catch (e) { showAlert(e.message); return; }
    }

    app.appendChild(bc(
        { label: "Customers", hash: "/admin" },
        ...(isNew ? [{ label: "New" }] : [{ label: customer.name, hash: `/admin/customers/${customerId}` }, { label: "Edit" }]),
    ));
    app.appendChild(phead({
        eyebrow: isNew ? "New customer" : "Edit customer",
        title: isNew ? "Onboard a customer" : customer.name,
    }));

    const form = h("form", { className: "form", onsubmit: async (e) => {
        e.preventDefault();
        const name = form.querySelector('[name="name"]').value.trim();
        const domain = form.querySelector('[name="domain"]').value.trim();
        const description = form.querySelector('[name="description"]').value.trim();
        try {
            if (isNew) {
                const c = await api("/api/admin/customers", { method: "POST", body: JSON.stringify({ name, domain, description }) });
                navigate(`/admin/customers/${c.id}`);
            } else {
                await api(`/api/admin/customers/${customerId}`, { method: "PATCH", body: JSON.stringify({ name, domain, description }) });
                navigate(`/admin/customers/${customerId}`);
            }
        } catch (err) { showAlert(err.message); }
    }},
        h("h3", {}, "Identity"),
        h("label", { htmlFor: "n" }, "Name"),
        h("input", { id: "n", name: "name", type: "text", required: true, value: customer.name || "" }),
        h("label", { htmlFor: "d" }, "Domain"),
        h("input", { id: "d", name: "domain", type: "text", required: true, pattern: "[a-z0-9.-]+", className: "mono", value: customer.domain || "" }),
        h("label", { htmlFor: "ds" }, "Description"),
        h("input", { id: "ds", name: "description", type: "text", value: customer.description || "" }),
    );
    app.appendChild(form);
    app.appendChild(h("div", { className: "btn-row" },
        h("button", { className: "btn primary", onclick: () => form.requestSubmit() }, isNew ? "Create customer" : "Save changes"),
        h("a", { className: "btn ghost", href: isNew ? "#/admin" : `#/admin/customers/${customerId}` }, "Cancel"),
    ));
}

// ---------- Admin: contract detail ----------

async function renderAdminContractDetail(contractId) {
    clear(app);
    app.className = "page";

    try {
        const contract = await api(`/api/admin/contracts/${contractId}`);
        app.appendChild(bc(
            { label: "Customers", hash: "/admin" },
            { label: contract.customer.name, hash: `/admin/customers/${contract.customer.id}` },
            { label: contract.contract_number },
        ));
        app.appendChild(phead({
            eyebrow: "Contract",
            title: contract.contract_number,
            lead: `${contract.customer.name} · ${contract.users.length} user${contract.users.length === 1 ? "" : "s"} with portal access.`,
        }));

        // Description
        app.appendChild(h("div", { className: "slbl first" }, "Description"));
        const descForm = h("form", { className: "form", onsubmit: async (e) => {
            e.preventDefault();
            const description = descForm.querySelector('[name="description"]').value.trim();
            try {
                await api(`/api/admin/contracts/${contractId}`, { method: "PATCH", body: JSON.stringify({ description }) });
                showAlert("Description saved", "success");
            } catch (err) { showAlert(err.message); }
        }},
            h("label", { htmlFor: "d" }, "Contract description"),
            (() => {
                const t = h("textarea", { id: "d", name: "description", className: "sans", style: "min-height:64px" });
                t.value = contract.description || "";
                return t;
            })(),
            h("div", { className: "btn-row" },
                h("button", { type: "submit", className: "btn primary sm" }, "Save description"),
            ),
        );
        app.appendChild(descForm);

        // Rebate
        app.appendChild(h("div", { className: "slbl" }, "Rebate"));
        const rebateForm = h("form", { className: "form", onsubmit: async (e) => {
            e.preventDefault();
            const v = rebateForm.querySelector('[name="rebate"]').value.trim();
            try {
                await api(`/api/admin/contracts/${contractId}/rebate`, { method: "PUT", body: JSON.stringify({ rebate_percent: parseFloat(v) }) });
                renderAdminContractDetail(contractId);
            } catch (err) { showAlert(err.message); }
        }},
            h("label", { htmlFor: "r" }, "Rebate percent"),
            h("div", { className: "input-suffix" },
                h("input", { id: "r", name: "rebate", type: "number", min: "0", max: "100", step: "0.01", value: contract.rebate_percent != null ? String(contract.rebate_percent) : "" }),
                h("span", { className: "suffix" }, "% off all line items"),
            ),
            h("p", { className: "hint" }, "Applied as a flat discount on every billed line for this contract. Set to 0 to remove."),
            h("div", { className: "btn-row" },
                h("button", { type: "submit", className: "btn primary sm" }, "Apply rebate"),
                contract.rebate_percent != null
                    ? h("button", { type: "button", className: "btn ghost sm", onclick: async () => {
                        try {
                            await api(`/api/admin/contracts/${contractId}/rebate`, { method: "DELETE" });
                            renderAdminContractDetail(contractId);
                        } catch (err) { showAlert(err.message); }
                    }}, "Remove rebate")
                    : null,
            ),
        );
        app.appendChild(rebateForm);

        // Price overrides
        let overrides = [];
        try { overrides = await api(`/api/admin/contracts/${contractId}/pricing`); } catch {}
        let globalPrices = [];
        try { globalPrices = await api("/api/admin/pricing"); } catch {}
        const globalPriceByResource = new Map();
        for (const price of globalPrices) {
            if (!globalPriceByResource.has(price.resource_type)) {
                globalPriceByResource.set(price.resource_type, price);
            }
        }
        app.appendChild(slbl("Price overrides", overrides.length, {
            help: { label: "How does pricing work?", href: "#/admin/pricing/docs" },
        }));
        if (!overrides.length) {
            app.appendChild(h("p", { className: "hint", style: "margin-bottom:10px" }, "Using global default prices."));
        } else {
            for (const o of overrides) {
                const unit = globalPriceByResource.get(o.resource_type)?.unit || "unit";
                app.appendChild(h("div", { className: "pt-row" },
                    h("div", {},
                        h("div", { className: "name" }, o.resource_type),
                        h("div", { className: "meta" }, "Override"),
                    ),
                    h("div", { className: "price" }, `${Number(o.unit_price).toFixed(4)} SEK `, h("span", { className: "unit" }, `/ ${unit}`)),
                    h("button", { className: "btn ghost tiny", onclick: async () => {
                        if (!confirm(`Remove override for ${o.resource_type}?`)) return;
                        try {
                            await api(`/api/admin/contracts/${contractId}/pricing/${encodeURIComponent(o.resource_type)}`, { method: "DELETE" });
                            renderAdminContractDetail(contractId);
                        } catch (err) { showAlert(err.message); }
                    }}, "Remove"),
                ));
            }
        }

        const globalProductPrices = [...globalPriceByResource.values()];
        if (globalProductPrices.length) {
            const sel = h("select", { id: "rt", name: "resource_type", required: true },
                h("option", { value: "" }, "— Select resource type —"),
                ...globalProductPrices.map(p => h("option", { value: p.resource_type }, `${p.resource_type} (${p.unit_price} SEK / ${p.unit})`)),
            );
            const addOverride = h("form", { className: "form", style: "margin-top:14px", onsubmit: async (e) => {
                e.preventDefault();
                const rt = sel.value;
                const price = addOverride.querySelector('[name="unit_price"]').value.trim();
                if (!rt) return;
                try {
                    await api(`/api/admin/contracts/${contractId}/pricing/${encodeURIComponent(rt)}`, {
                        method: "PUT", body: JSON.stringify({ resource_type: rt, unit_price: parseFloat(price) }),
                    });
                    renderAdminContractDetail(contractId);
                } catch (err) { showAlert(err.message); }
            }},
                h("h3", {}, "Add override"),
                h("div", { className: "row-2" },
                    h("div", { className: "field" },
                        h("label", { htmlFor: "rt" }, "Resource type"),
                        sel,
                    ),
                    h("div", { className: "field" },
                        h("label", { htmlFor: "up" }, "Unit price (SEK)"),
                        h("input", { id: "up", name: "unit_price", type: "number", step: "0.0001", min: "0", required: true, placeholder: "0.00" }),
                    ),
                ),
                h("div", { className: "btn-row" },
                    h("button", { type: "submit", className: "btn primary sm" }, "Add override"),
                ),
            );
            app.appendChild(addOverride);
        } else {
            app.appendChild(h("p", { className: "hint" }, "Configure global prices first (Admin → Pricing) before adding overrides."));
        }

        // Portal access
        app.appendChild(slbl("Portal access", `${contract.users.length} user${contract.users.length === 1 ? "" : "s"}`));
        if (!contract.users.length) {
            app.appendChild(emptyState("No users have access yet."));
        } else {
            const ul = h("ul", { className: "ilist" });
            for (const sub of contract.users) {
                ul.appendChild(h("li", {},
                    h("span", {}, sub),
                    h("button", { className: "btn ghost tiny", onclick: async () => {
                        if (!confirm(`Revoke access for ${sub}?`)) return;
                        try {
                            await api(`/api/admin/contracts/${contractId}/users/${encodeURIComponent(sub)}`, { method: "DELETE" });
                            renderAdminContractDetail(contractId);
                        } catch (err) { showAlert(err.message); }
                    }}, "Revoke"),
                ));
            }
            app.appendChild(ul);
        }

        const grant = h("form", { className: "form", style: "margin-top:10px", onsubmit: async (e) => {
            e.preventDefault();
            const v = grant.querySelector('[name="user_sub"]').value.trim();
            if (!v) return;
            try {
                await api(`/api/admin/contracts/${contractId}/users`, { method: "POST", body: JSON.stringify({ user_sub: v }) });
                renderAdminContractDetail(contractId);
            } catch (err) { showAlert(err.message); }
        }},
            h("label", { htmlFor: "u" }, "Grant access to user"),
            h("div", { className: "input-suffix" },
                h("input", { id: "u", name: "user_sub", type: "text", required: true, placeholder: "user@idp" }),
                h("button", { type: "submit", className: "btn primary sm" }, "Grant"),
            ),
            h("p", { className: "hint" }, "User is identified by their SWAMID ", h("code", {}, "sub"), " claim — typically their eduPersonPrincipalName."),
        );
        app.appendChild(grant);

        // --- Danger zone: relocation & deletion ---
        const dz = h("div", { className: "danger-zone" });
        dz.appendChild(h("div", { className: "dz-head" }, "Danger zone"));
        dz.appendChild(h("p", { className: "hint" },
            "Relocation and deletion. Moves re-point billing; renames re-point every project; deletion is permanent."));

        let customers = [], allContracts = [], projects = [];
        try { customers = await api("/api/admin/customers"); } catch {}
        try { allContracts = await api("/api/admin/contracts"); } catch {}
        try {
            projects = await api(`/api/contracts/${encodeURIComponent(contract.contract_number)}/projects`);
        } catch {}
        const customersById = Object.fromEntries((customers || []).map(c => [c.id, c]));

        // Projects — move each to another contract.
        dz.appendChild(h("div", { className: "slbl" }, "Projects", h("span", { className: "count" }, String(projects.length))));
        if (!projects.length) {
            dz.appendChild(h("p", { className: "hint" }, "No projects on this contract."));
        } else {
            const otherContracts = (allContracts || []).filter(c => c.contract_number !== contract.contract_number);
            for (const p of projects) {
                const row = h("div", { className: "dz-row" });
                row.appendChild(h("a", { className: "dz-row-name link",
                    href: `#/contracts/${encodeURIComponent(contract.contract_number)}/projects/${encodeURIComponent(p.resource_name)}` },
                    p.name));
                if (!otherContracts.length) {
                    row.appendChild(h("span", { className: "hint" }, "No other contract to move to."));
                } else {
                    const sel = h("select", {},
                        ...otherContracts.map(c => {
                            const cust = customersById[c.customer_id];
                            return h("option", { value: c.contract_number },
                                `${c.contract_number}${cust ? " — " + cust.name : ""}`);
                        }),
                    );
                    const btn = h("button", { className: "btn danger tiny", onclick: async () => {
                        const target = sel.value;
                        if (!target) return;
                        if (!confirm(`Move project ${p.name} to contract ${target}? It keeps its name and OpenStack resources; only billing follows the new contract.`)) return;
                        try {
                            await api(`/api/admin/projects/${encodeURIComponent(p.resource_name)}/move`, {
                                method: "POST", body: JSON.stringify({ contract_number: target }),
                            });
                            renderAdminContractDetail(contractId);
                        } catch (err) { showAlert(err.message); }
                    }}, "Move");
                    row.appendChild(sel);
                    row.appendChild(btn);
                }
                dz.appendChild(row);
            }
        }

        // Rename contract.
        dz.appendChild(h("div", { className: "slbl" }, "Rename contract"));
        const renameForm = h("form", { className: "form", onsubmit: async (e) => {
            e.preventDefault();
            const v = renameForm.querySelector('[name="contract_number"]').value.trim();
            if (!v || v === contract.contract_number) return;
            if (!confirm(`Rename contract ${contract.contract_number} to ${v}? All ${projects.length} project(s) will be re-pointed to the new number.`)) return;
            try {
                await api(`/api/admin/contracts/${contractId}/rename`, {
                    method: "POST", body: JSON.stringify({ contract_number: v }),
                });
                renderAdminContractDetail(contractId);
            } catch (err) { showAlert(err.message); }
        }},
            h("p", { className: "hint" }, "Changes the contract number and re-points all its projects. Names and OpenStack resources are unaffected; only the billing identifier changes."),
            h("label", { htmlFor: "cn" }, "New contract number"),
            h("div", { className: "input-suffix" },
                (() => {
                    const i = h("input", { id: "cn", name: "contract_number", type: "text", required: true, pattern: "[A-Za-z0-9-]+" });
                    i.value = contract.contract_number;
                    return i;
                })(),
                h("button", { type: "submit", className: "btn danger sm" }, "Rename"),
            ),
        );
        dz.appendChild(renameForm);

        // Move to another customer.
        dz.appendChild(h("div", { className: "slbl" }, "Move to another customer"));
        const moveTargets = (customers || []).filter(c => c.id !== contract.customer.id);
        if (!moveTargets.length) {
            dz.appendChild(h("p", { className: "hint" }, "No other customers to move this contract to."));
        } else {
            const warn = h("p", { className: "hint", style: "display:none" });
            const updateWarn = (cust) => {
                if (cust && cust.domain !== contract.customer.domain) {
                    warn.style.display = "";
                    warn.textContent = `Note: ${cust.name} uses domain “${cust.domain}”, but existing projects keep their current names (which embed “${contract.customer.domain}”). The name is cosmetic — resources and access are unaffected.`;
                } else {
                    warn.style.display = "none";
                }
            };
            const sel = h("select", { id: "move-customer", name: "customer_id" },
                ...moveTargets.map(c => h("option", { value: String(c.id) }, `${c.name} (${c.domain})`)),
            );
            sel.addEventListener("change", () => updateWarn(moveTargets.find(c => String(c.id) === sel.value)));
            const moveForm = h("form", { className: "form", onsubmit: async (e) => {
                e.preventDefault();
                const customerId = parseInt(sel.value, 10);
                const cust = moveTargets.find(c => c.id === customerId);
                if (!confirm(`Move contract ${contract.contract_number} to ${cust ? cust.name : "the selected customer"}?`)) return;
                try {
                    await api(`/api/admin/contracts/${contractId}/move`, {
                        method: "POST", body: JSON.stringify({ customer_id: customerId }),
                    });
                    renderAdminContractDetail(contractId);
                } catch (err) { showAlert(err.message); }
            }},
                h("label", { htmlFor: "move-customer" }, "Target customer"),
                sel,
                warn,
                h("div", { className: "btn-row" },
                    h("button", { type: "submit", className: "btn danger sm" }, "Move contract"),
                ),
            );
            updateWarn(moveTargets[0]);
            dz.appendChild(moveForm);
        }

        // Delete contract.
        dz.appendChild(h("div", { className: "slbl" }, "Delete contract"));
        dz.appendChild(h("p", { className: "hint" }, "Permanently removes this contract. All its projects must be deleted first."));
        dz.appendChild(h("div", { className: "btn-row" },
            h("button", { className: "btn danger sm", onclick: async () => {
                if (!confirm(`Delete contract ${contract.contract_number}? All projects must be deleted first.`)) return;
                try {
                    await api(`/api/admin/contracts/${contractId}`, { method: "DELETE" });
                    navigate(`/admin/customers/${contract.customer.id}`);
                } catch (err) { showAlert(err.message); }
            }}, "Delete contract"),
        ));

        app.appendChild(dz);
    } catch (e) { showAlert(e.message); }
}

async function renderAdminEditContract(contractId) {
    clear(app);
    app.className = "page narrow-form";
    try {
        const contract = await api(`/api/admin/contracts/${contractId}`);
        app.appendChild(bc(
            { label: "Customers", hash: "/admin" },
            { label: contract.customer.name, hash: `/admin/customers/${contract.customer.id}` },
            { label: contract.contract_number, hash: `/admin/contracts/${contractId}` },
            { label: "Edit" },
        ));
        app.appendChild(phead({ eyebrow: "Edit contract", title: contract.contract_number }));

        const form = h("form", { className: "form", onsubmit: async (e) => {
            e.preventDefault();
            const description = form.querySelector('[name="description"]').value.trim();
            try {
                await api(`/api/admin/contracts/${contractId}`, { method: "PATCH", body: JSON.stringify({ description }) });
                navigate(`/admin/contracts/${contractId}`);
            } catch (err) { showAlert(err.message); }
        }},
            h("label", {}, "Contract number"),
            h("input", { value: contract.contract_number, disabled: true, className: "mono" }),
            h("label", { htmlFor: "d" }, "Description"),
            h("input", { id: "d", name: "description", type: "text", value: contract.description || "" }),
        );
        app.appendChild(form);
        app.appendChild(h("div", { className: "btn-row" },
            h("button", { className: "btn primary", onclick: () => form.requestSubmit() }, "Save changes"),
            h("a", { className: "btn ghost", href: `#/admin/contracts/${contractId}` }, "Cancel"),
        ));
    } catch (e) { showAlert(e.message); }
}

// ---------- Admin: pricing ----------

async function renderAdminPricing() {
    clear(app);
    app.className = "page";
    app.appendChild(phead({
        eyebrow: "Admin",
        title: "Global pricing",
        lead: "Default unit prices for every billable resource. Contracts may override individual lines on their own configuration page.",
        actions: [h("a", { className: "btn ghost sm", href: "#/admin/pricing/docs" }, "View pricing docs →")],
    }));

    let prices = [];
    try { prices = await api("/api/admin/pricing"); } catch (e) { showAlert(e.message); }
    app.appendChild(slbl("Configured prices", prices.length));
    if (!prices.length) {
        app.appendChild(emptyState("No prices configured yet."));
    } else {
        for (const p of prices) {
            const meta = p.metadata_field && p.metadata_value ? `${p.metadata_field} = ${p.metadata_value}` : "Base price";
            app.appendChild(h("div", { className: "pt-row" },
                h("div", {},
                    h("div", { className: "name" }, p.resource_type),
                    h("div", { className: "meta" }, meta),
                ),
                h("div", { className: "price" }, `${Number(p.unit_price).toFixed(6)} SEK `, h("span", { className: "unit" }, "/ " + p.unit)),
                h("button", { className: "btn ghost tiny", onclick: async () => {
                    if (!confirm(`Remove price for ${p.resource_type}?`)) return;
                    try { await api(`/api/admin/pricing/${p.id}`, { method: "DELETE" }); renderAdminPricing(); }
                    catch (err) { showAlert(err.message); }
                }}, "Remove"),
            ));
        }
    }

    // Add resource price form (Gnocchi-aware)
    let metrics = [];
    try { metrics = await api("/api/admin/pricing/metrics"); } catch {}
    const metricUnits = {};
    const metricMeta = {};
    for (const m of metrics) {
        metricUnits[m.metric_type] = m.unit || "";
        metricMeta[m.metric_type] = m.metadata_fields || [];
    }

    const metaContainer = h("div", { id: "meta-fields", style: "display:none;margin-top:14px" });

    const metricSelect = metrics.length
        ? h("select", { id: "rt", name: "resource_type", required: true, onchange: (e) => {
            const rt = e.target.value;
            const fields = metricMeta[rt] || [];
            clear(metaContainer);
            if (fields.length && fields[0].values?.length) {
                const field = fields[0];
                metaContainer.style.display = "block";
                metaContainer.appendChild(h("input", { type: "hidden", name: "metadata_field", value: field.field }));
                metaContainer.appendChild(h("label", {}, `${field.field} (optional — leave blank for base price)`));
                metaContainer.appendChild(h("select", { name: "metadata_value" },
                    h("option", { value: "" }, "— All (base price) —"),
                    ...field.values.map(v => h("option", { value: v }, v)),
                ));
            } else {
                metaContainer.style.display = "none";
            }
        }},
            h("option", { value: "" }, "— Select metric —"),
            ...metrics.map(m => h("option", { value: m.metric_type }, `${m.metric_type} (${m.unit})`)),
        )
        : h("input", { id: "rt", name: "resource_type", type: "text", required: true, placeholder: "metric type (Gnocchi unavailable)" });

    const addForm = h("form", { className: "form", style: "margin-top:18px", onsubmit: async (e) => {
        e.preventDefault();
        const rt = (addForm.querySelector('[name="resource_type"]').value || "").trim();
        const price = addForm.querySelector('[name="unit_price"]').value.trim();
        const unit = metricUnits[rt] || "unit";
        const metaField = addForm.querySelector('[name="metadata_field"]');
        const metaValue = addForm.querySelector('[name="metadata_value"]');
        if (!rt) return;
        const body = { resource_type: rt, unit_price: parseFloat(price), unit };
        if (metaField && metaValue && metaValue.value) {
            body.metadata_field = metaField.value;
            body.metadata_value = metaValue.value;
        }
        try {
            await api("/api/admin/pricing", { method: "POST", body: JSON.stringify(body) });
            renderAdminPricing();
        } catch (err) { showAlert(err.message); }
    }},
        h("h3", {}, "Add resource price"),
        h("div", { className: "row-2" },
            h("div", { className: "field" },
                h("label", { htmlFor: "rt" }, "Resource type"),
                metricSelect,
            ),
            h("div", { className: "field" },
                h("label", { htmlFor: "up" }, "Unit price (SEK per displayed unit)"),
                h("input", { id: "up", name: "unit_price", type: "number", step: "0.000001", min: "0", required: true, placeholder: "0.000000" }),
            ),
        ),
        metaContainer,
        h("p", { className: "hint" }, "VMs use started one-hour UTC buckets. Storage is normalized to GB-month."),
        h("div", { className: "btn-row" },
            h("button", { type: "submit", className: "btn primary sm" }, "Add price"),
        ),
    );
    app.appendChild(addForm);

    if (!metrics.length) {
        app.appendChild(h("p", { className: "hint" }, "Could not connect to Gnocchi to discover available metrics. You can enter metric types manually."));
    }
}

// ---------- Pricing docs ----------

function renderPricingDocs() {
    clear(app);
    app.className = "page narrow";
    app.appendChild(phead({
        eyebrow: "Pricing reference",
        title: "How billing works",
        lead: "SUNET Cloud bills by metered resource consumption per project, rolled up to the project's contract.",
    }));

    const doc = h("div", { className: "doc" });
    doc.innerHTML = `
        <h3>Overview</h3>
        <p>The billing system queries <strong>Gnocchi</strong> (the metrics database) for resource usage data,
        then applies the prices you configure here to calculate costs for each contract.</p>
        <p>The pipeline is: <code>Ceilometer</code> (collects metrics) → <code>Gnocchi</code> (stores time-series data)
        → <code>Portal billing</code> (queries usage, applies prices, generates CSV).</p>

        <h3>How metering works</h3>
        <p>Ceilometer polls OpenStack services at a fixed interval and stores measurements in Gnocchi.
        Each measurement is a <strong>data point</strong> — one sample taken at one point in time.</p>
        <p>The billing system requests history-aware <strong>one-hour UTC buckets</strong>. For a virtual
        machine, every non-empty CPU bucket counts as one started hour for that VM's flavor. The cumulative
        CPU value is not used as the quantity.</p>
        <p>A VM running during any part of an hour is charged for that hour. The shutdown hour is charged;
        later hours without CPU samples are not. A stop/start cycle in one hour counts once.</p>

        <h3>The four-step calculation</h3>
        <ol>
            <li>Each <strong>project</strong> belongs to exactly one <strong>contract</strong>.</li>
            <li>Every VM is grouped separately by its historical flavor; storage resources are measured separately by size.</li>
            <li>Each metered line is multiplied by the resource's <strong>unit price</strong>. By default this is the global price; if the contract has a price override for that resource, the override wins.</li>
            <li>The contract's <strong>rebate percent</strong> is applied to the line total. The result appears on the monthly CSV.</li>
        </ol>

        <h3>Resource types</h3>
        <table>
            <thead><tr><th>Metric</th><th>What it measures</th><th>Priced per</th></tr></thead>
            <tbody>
                <tr><td><code>instance</code></td><td>Started VM hours by historical flavor</td><td>flavor-hour</td></tr>
                <tr><td><code>volume.size</code></td><td>Time-weighted block storage size</td><td>GB-month</td></tr>
                <tr><td><code>volume.snapshot.size</code></td><td>Time-weighted logical snapshot size</td><td>GB-month</td></tr>
                <tr><td><code>volume.backup.size</code></td><td>Time-weighted logical backup size</td><td>GB-month</td></tr>
                <tr><td><code>radosgw.objects.size</code></td><td>Time-weighted S3/object storage size</td><td>GB-month</td></tr>
            </tbody>
        </table>
        <p>Volume, snapshot, and backup storage use the logical provisioned size reported by Cinder.
        Snapshot and backup quantities therefore describe customer-visible GB-month, not compressed,
        deduplicated, or physical backend consumption.</p>

        <h3>Metadata-based pricing</h3>
        <p>Some metrics have <strong>metadata fields</strong> that allow more granular pricing. For example,
        the <code>instance</code> metric includes <code>flavor_name</code>, so you can set different prices
        for different VM sizes. Gnocchi records <code>volume_type</code> as a Cinder ID; the portal
        resolves that ID to the active Cinder type name before matching a price. The deployed block
        storage type is <code>rbd1</code>.</p>
        <p>When billing, the system matches prices in this order:</p>
        <ol>
            <li><strong>Specific price</strong> — matches both the metric type AND the metadata value.</li>
            <li><strong>Base price</strong> — matches just the metric type, used as fallback.</li>
        </ol>

        <h3>Worked example</h3>
        <div class="example">
            <p><strong>Example: VM flavor b2.c4r8 at 1,095 SEK/month</strong></p>
            <p>1. Hourly rate: 1,095 ÷ 730 = <strong>1.50 SEK/hour</strong></p>
            <p>2. Resource type: <code>instance</code></p>
            <p>3. Metadata: <code>flavor_name = b2.c4r8</code></p>
            <p>4. Unit price: <code>1.50</code></p>
            <p>5. 730 started flavor-hours × 1.50 = 1,095 SEK</p>
        </div>

        <h3>Telemetry limitations</h3>
        <p>Ceilometer polls every five minutes. A resource created and deleted entirely between polls can
        be missed, and a telemetry interruption can look like stopped or absent usage. Retention changes do
        not recreate measurements that were never stored.</p>

        <h3>Contract overrides and rebates</h3>
        <p><strong>Price overrides</strong> let you set a different unit price for a specific contract and billing product.</p>
        <p><strong>Rebates</strong> are a percentage discount applied after the price calculation:
        <code>quantity × unit_price × (1 − rebate%/100) = cost</code></p>

        <h3>Delivery</h3>
        <p>Billing exports run as scheduled <strong>billing jobs</strong>. A job covers one or more contracts,
        runs on a cron schedule, and delivers CSVs by WebDAV or email. Filenames support <code>{contract}</code>,
        <code>{year}</code> and <code>{month}</code> tokens.</p>

        <h3>Re-runs</h3>
        <p>Manual runs of past periods are idempotent — they overwrite the previous CSV in place. Use this
        to correct a delivery that failed or to re-issue with updated pricing.</p>
    `;
    app.appendChild(doc);
}

// ---------- Admin: all billing jobs ----------

async function renderAdminBillingJobs() {
    clear(app);
    app.className = "page";
    app.appendChild(phead({
        eyebrow: "Admin",
        title: "All billing jobs",
        lead: "Every billing export configured across all users.",
    }));

    try {
        const jobs = await api("/api/billing/jobs?all=true");
        app.appendChild(slbl("All jobs", jobs.length));
        if (!jobs.length) {
            app.appendChild(emptyState("No billing jobs configured."));
            return;
        }
        for (const j of jobs) {
            const target = j.delivery_method === "webdav"
                ? (j.delivery_config?.url || "WebDAV")
                : (j.delivery_config?.recipient || "email");
            app.appendChild(h("a", { className: "card link", href: `#/billing/${j.id}` },
                h("div", { className: "card-head" },
                    h("h3", {}, j.name),
                    j.enabled ? badge("Enabled", "ready") : badge("Disabled", "neutral"),
                ),
                h("div", { className: "meta" }, `Owner: ${j.owner_sub} · ${j.delivery_method} → ${target}`),
                h("div", { className: "meta mono" }, j.schedule),
            ));
        }
    } catch (e) { showAlert(e.message); }
}

// ========== Cluster helpers ==========

const ADDON_DISPLAY_NAMES = { jupyterhub: "JupyterHub" };

function statusBadge(status) {
    if (status === "active") return badge("active", "ok");
    if (status === "revoked") return badge("revoked", "err");
    if (status === "expired") return badge("expired", "warn");
    if (status === "pending") return badge("pending", "warn");
    if (status === "applied") return badge("applied", "ok");
    if (status === "denied") return badge("denied", "err");
    return badge(status || "?", "neutral");
}

function daysUntil(iso) {
    if (!iso) return null;
    const ms = new Date(iso).getTime() - Date.now();
    return Math.floor(ms / (1000 * 60 * 60 * 24));
}

function formatRequestSummary(r) {
    const p = r.payload || {};
    if (r.request_type === "addon") {
        const verb = p.action === "enable" ? "Enable" : "Disable";
        const name = ADDON_DISPLAY_NAMES[p.addon_type] || p.addon_type || "addon";
        return `${verb} ${name} addon`;
    }
    if (r.request_type === "resize") {
        const tgt = p.target_worker_groups;
        const before = p.before_worker_groups;
        if (before != null && tgt != null)
            return `Resize from ${before} to ${tgt} worker groups (${3 + 3 * tgt} servers total)`;
        if (tgt != null)
            return `Resize to ${tgt} worker groups (${3 + 3 * tgt} servers total)`;
        return "Resize";
    }
    if (r.request_type === "backup") {
        return p.action === "enable" ? "Enable backup" : "Disable backup";
    }
    return r.request_type;
}

// ========== Cluster: member-facing ==========

async function renderClusters() {
    clear(app); app.className = "page";
    app.appendChild(bc(
        { label: "Contracts", hash: "/contracts" },
        { label: "All clusters" },
    ));
    app.appendChild(phead({
        eyebrow: "Customer",
        title: "All clusters",
        lead: "Tenant Kubernetes clusters you have access to across every contract. Open a contract page from My Contracts to see clusters scoped to that contract.",
    }));
    try {
        const clusters = await api("/api/clusters");
        app.appendChild(slbl("Clusters", clusters.length));
        if (!clusters.length) {
            app.appendChild(emptyState("You don't have access to any clusters yet."));
            return;
        }
        for (const c of clusters) {
            app.appendChild(h("a", { className: "card link", href: `#/clusters/${encodeURIComponent(c.slug)}` },
                h("div", { className: "card-head" },
                    h("h3", {}, c.name),
                    c.provisioned_at ? badge("provisioned", "ok") : badge("pending", "warn"),
                ),
                h("div", { className: "meta" }, `${c.size_label} — ${c.total_servers} servers (3 controllers + ${3 * c.worker_groups} workers)`),
                h("div", { className: "meta mono" }, `Contract: ${c.contract_number} · Role: ${c.caller_role || "?"}`
                    + (c.active_addons.length ? ` · Addons: ${c.active_addons.join(", ")}` : "")),
            ));
        }
    } catch (e) { showAlert(e.message); }
}

async function renderClusterDetail(slug) {
    clear(app); app.className = "page";
    try {
        const cluster = await api(`/api/clusters/${slug}`);
        const cn = encodeURIComponent(cluster.contract_number || "");
        const isCustomerAdmin = cluster.caller_role === "customer_admin" || cluster.caller_role === "sunet_admin";

        app.appendChild(bc(
            { label: "Contracts", hash: "/contracts" },
            { label: cluster.contract_number, hash: `/contracts/${cn}/projects` },
            { label: slug },
        ));
        app.appendChild(phead({
            eyebrow: `Cluster · ${cluster.size_label}`,
            title: cluster.name,
            lead: cluster.provisioned_at ? "Provisioned and ready." : "Not yet provisioned — credential issuance is disabled.",
            actions: isCustomerAdmin
                ? [h("a", { className: "btn ghost sm", href: `#/clusters/${encodeURIComponent(slug)}/users` }, "Manage users")]
                : null,
        }));

        // Overview
        app.appendChild(h("div", { className: "slbl first" }, "Overview"));
        const overviewRows = [
            kvRowMono("Slug", cluster.slug),
            kvRow("Servers", `${cluster.total_servers} (3 controllers + ${3 * cluster.worker_groups} workers)`),
            kvRow("Contract", h("a", { className: "link", href: `#/contracts/${cn}/projects` }, cluster.contract_number)),
            kvRow("Your role", cluster.caller_role || "—"),
        ];
        if (cluster.management_project_resource_name) {
            overviewRows.push(kvRow("Management project",
                h("a", { className: "link", href: `#/contracts/${cn}/projects/${encodeURIComponent(cluster.management_project_resource_name)}` },
                    cluster.management_project_resource_name)));
        }
        if (cluster.backup_project_resource_name) {
            overviewRows.push(kvRow("Backup project",
                h("a", { className: "link", href: `#/contracts/${cn}/projects/${encodeURIComponent(cluster.backup_project_resource_name)}` },
                    cluster.backup_project_resource_name)));
        }
        if (cluster.active_addons.length) {
            overviewRows.push(kvRow("Active addons", cluster.active_addons.join(", ")));
        }
        app.appendChild(kv(...overviewRows));

        const requests = await api(`/api/clusters/${slug}/requests`);

        // Credentials
        app.appendChild(slbl("My credentials"));
        if (!cluster.provisioned_at) {
            app.appendChild(emptyState("Cluster is not yet provisioned — credential issuance is disabled."));
        } else {
            const issueForm = h("form", { className: "card", style: "margin-bottom:12px",
                onsubmit: async (e) => {
                    e.preventDefault();
                    const label = e.target.querySelector('[name="label"]').value.trim();
                    const ttlRaw = e.target.querySelector('[name="ttl_days"]').value.trim();
                    const body = { label };
                    if (ttlRaw) body.ttl_days = parseInt(ttlRaw, 10);
                    try {
                        const issued = await api(`/api/clusters/${slug}/credentials`, {
                            method: "POST", body: JSON.stringify(body),
                        });
                        showIssuedKubeconfig(issued);
                        renderClusterDetail(slug);
                    } catch (err) { showAlert(err.message); }
                }},
                h("div", { className: "card-head" }, h("h3", {}, "Issue new kubeconfig")),
                h("div", { className: "meta", style: "margin-top:8px" },
                    h("label", { style: "display:block;margin-bottom:6px;font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:1px" }, "Label"),
                    h("input", { name: "label", required: true, maxlength: "128", placeholder: "laptop", style: "width:100%;padding:10px;border:1px solid var(--line);border-radius:6px;font-family:inherit" }),
                    h("label", { style: "display:block;margin-top:10px;margin-bottom:6px;font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:1px" }, "TTL in days (optional, default 365)"),
                    h("input", { name: "ttl_days", type: "number", min: "1", max: "3650", style: "width:100%;padding:10px;border:1px solid var(--line);border-radius:6px;font-family:inherit" }),
                    h("div", { className: "btn-row" },
                        h("button", { type: "submit", className: "btn primary sm" }, "Issue kubeconfig"),
                    ),
                ),
            );
            app.appendChild(issueForm);

            const creds = await api(`/api/clusters/${slug}/credentials`);
            if (!creds.length) {
                app.appendChild(emptyState("You haven't issued any credentials yet."));
            } else {
                for (const c of creds) {
                    const expiryDays = daysUntil(c.expires_at);
                    const expiryWarn = c.status === "active" && expiryDays !== null && expiryDays < 30;
                    const card = h("div", { className: "card" },
                        h("div", { className: "card-head" },
                            h("h3", {}, c.label),
                            statusBadge(c.status),
                        ),
                        h("div", { className: "meta" }, `Issued ${fmtDay(c.created_at)} · expires ${fmtDay(c.expires_at)}` + (expiryWarn ? ` (in ${expiryDays} days)` : "")),
                        h("div", { className: "meta mono" }, `Serial: ${c.cert_serial.substring(0, 16)}…`),
                    );
                    if (c.status === "active") {
                        const row = h("div", { className: "btn-row" },
                            h("button", { className: "btn ghost tiny",
                                onclick: async () => {
                                    if (!confirm(`Rotate credential "${c.label}"? The old kubeconfig will stop working.`)) return;
                                    try {
                                        const issued = await api(`/api/clusters/${slug}/credentials/${c.id}/rotate`, { method: "POST" });
                                        showIssuedKubeconfig(issued);
                                        renderClusterDetail(slug);
                                    } catch (err) { showAlert(err.message); }
                                }}, "Rotate"),
                            h("button", { className: "btn danger tiny",
                                onclick: async () => {
                                    if (!confirm(`Revoke credential "${c.label}"? Immediate, can't be undone.`)) return;
                                    try {
                                        await api(`/api/clusters/${slug}/credentials/${c.id}`, { method: "DELETE" });
                                        renderClusterDetail(slug);
                                    } catch (err) { showAlert(err.message); }
                                }}, "Revoke"),
                        );
                        card.appendChild(row);
                    }
                    app.appendChild(card);
                }
            }
        }

        // Request a change (customer admins / SUNET admins only)
        if (isCustomerAdmin) {
            app.appendChild(slbl("Request a change"));
            app.appendChild(renderClusterRequestPanel(slug, cluster, requests));
        }

        // Request history
        app.appendChild(slbl("Request history", requests.length));
        if (!requests.length) {
            app.appendChild(emptyState("No requests yet."));
        } else {
            for (const r of requests) {
                app.appendChild(h("div", { className: "card" },
                    h("div", { className: "card-head" },
                        h("h3", {}, formatRequestSummary(r)),
                        statusBadge(r.status),
                    ),
                    h("div", { className: "meta" }, `Requested by ${r.requested_by_sub} on ${fmtDay(r.requested_at)}`),
                    r.applied_at ? h("div", { className: "meta" },
                        `${r.status === "applied" ? "Applied" : "Denied"} by ${r.applied_by_sub || "?"} on ${fmtDay(r.applied_at)}`) : null,
                    r.note ? h("div", { className: "meta" }, `Note: ${r.note}`) : null,
                ));
            }
        }
    } catch (e) { showAlert(e.message); }
}

function renderClusterRequestPanel(slug, cluster, requests) {
    const panel = h("div", { className: "card" });
    const pendingByType = (type, predicate = () => true) =>
        (requests || []).some(r => r.status === "pending" && r.request_type === type && predicate(r.payload || {}));

    // JupyterHub addon
    const jhActive = cluster.active_addons.includes("jupyterhub");
    const jhPending = pendingByType("addon", p => p.addon_type === "jupyterhub");
    const jhDisabled = jhActive || jhPending;
    const jhBtn = h("button", {
        className: "btn ghost sm", disabled: jhDisabled || undefined,
        onclick: async () => {
            if (!confirm("Request JupyterHub addon? SUNET ops will be notified by email.")) return;
            try {
                await api(`/api/clusters/${slug}/requests`, { method: "POST", body: JSON.stringify({
                    request_type: "addon",
                    payload: { action: "enable", addon_type: "jupyterhub" },
                })});
                renderClusterDetail(slug);
            } catch (err) { showAlert(err.message); }
        },
    }, jhActive ? "JupyterHub already enabled" : (jhPending ? "JupyterHub request pending" : "Request JupyterHub addon"));
    panel.appendChild(h("div", { style: "padding:6px 0" }, jhBtn));

    // Resize
    const resizePending = pendingByType("resize");
    const resizeRow = h("div", { style: "display:flex;gap:8px;align-items:center;padding:6px 0;flex-wrap:wrap" },
        h("div", { className: "meta", style: "min-width:0;flex:1" },
            `Resize cluster (current: ${cluster.worker_groups} worker groups, ${3 * cluster.worker_groups} workers)`),
        h("input", {
            name: "resize_target", type: "number",
            min: cluster.worker_groups + 1,
            placeholder: `> ${cluster.worker_groups}`,
            disabled: resizePending || undefined,
            style: "width:120px;padding:8px;border:1px solid var(--line);border-radius:6px;font-family:inherit",
        }),
        h("button", {
            className: "btn ghost sm", disabled: resizePending || undefined,
            onclick: async () => {
                const input = panel.querySelector('[name="resize_target"]');
                const target = parseInt(input.value, 10);
                if (!target || target <= cluster.worker_groups) {
                    showAlert("Target must be greater than current."); return;
                }
                if (!confirm(`Request resize to ${target} worker groups (${3 + 3 * target} servers total)?`)) return;
                try {
                    await api(`/api/clusters/${slug}/requests`, { method: "POST", body: JSON.stringify({
                        request_type: "resize",
                        payload: { target_worker_groups: target },
                    })});
                    renderClusterDetail(slug);
                } catch (err) { showAlert(err.message); }
            },
        }, resizePending ? "Resize request pending" : "Request resize"),
    );
    panel.appendChild(resizeRow);

    // Backup
    const backupEnabled = !!cluster.backup_project_resource_name;
    const backupPending = pendingByType("backup");
    const backupBtn = h("button", {
        className: "btn ghost sm", disabled: backupPending || undefined,
        onclick: async () => {
            const action = backupEnabled ? "disable" : "enable";
            if (!confirm(`Request to ${action} backup?`)) return;
            try {
                await api(`/api/clusters/${slug}/requests`, { method: "POST", body: JSON.stringify({
                    request_type: "backup",
                    payload: { action },
                })});
                renderClusterDetail(slug);
            } catch (err) { showAlert(err.message); }
        },
    }, backupPending ? "Backup request pending" : (backupEnabled ? "Request to disable backup" : "Request to enable backup"));
    panel.appendChild(h("div", { style: "padding:6px 0" }, backupBtn));

    return panel;
}

function showIssuedKubeconfig(issued) {
    const overlay = h("div", {
        style: "position:fixed;inset:0;background:rgba(0,0,0,0.55);display:flex;align-items:center;justify-content:center;z-index:100",
    });
    const close = () => overlay.remove();
    const blob = new Blob([issued.kubeconfig], { type: "text/yaml" });
    const url = URL.createObjectURL(blob);
    const dialog = h("div", { className: "card", style: "max-width:640px;width:92%;max-height:90vh;overflow:auto" },
        h("div", { className: "card-head" }, h("h3", {}, "Kubeconfig issued")),
        h("div", { className: "meta" }, `Label: ${issued.label} · Expires: ${fmtDay(issued.expires_at)}`),
        h("div", { className: "meta", style: "margin-top:8px" },
            "Save the file below — it is shown only once and not retrievable later."),
        h("textarea", {
            readonly: true, value: issued.kubeconfig,
            style: "width:100%;height:200px;font-family:var(--mono);font-size:12px;margin:10px 0;padding:10px;border:1px solid var(--line);border-radius:6px",
        }),
        h("div", { className: "btn-row" },
            h("a", { className: "btn primary sm",
                href: url, download: `kubeconfig-${issued.cluster_slug}-${issued.label}.yaml` }, "Download"),
            h("button", { className: "btn ghost sm", onclick: close }, "Close"),
        ),
    );
    overlay.appendChild(dialog);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    document.body.appendChild(overlay);
}

async function renderClusterUsers(slug) {
    clear(app); app.className = "page";
    try {
        const cluster = await api(`/api/clusters/${slug}`);
        const users = await api(`/api/clusters/${slug}/users`);
        const isSunetAdmin = cluster.caller_role === "sunet_admin";
        const cn = encodeURIComponent(cluster.contract_number || "");

        app.appendChild(bc(
            { label: "Contracts", hash: "/contracts" },
            { label: cluster.contract_number, hash: `/contracts/${cn}/projects` },
            { label: slug, hash: `/clusters/${encodeURIComponent(slug)}` },
            { label: "Users" },
        ));
        app.appendChild(phead({
            eyebrow: "Cluster · users",
            title: "Cluster users",
            lead: "Customer admins manage their team members. SUNET admins can also mint other customer admins.",
        }));

        for (const u of users) {
            const canRemove = u.role !== "customer_admin" || isSunetAdmin;
            const card = h("div", { className: "card" },
                h("div", { className: "card-head" },
                    h("h3", {}, u.user_sub),
                    badge(u.role, "neutral"),
                ),
                h("div", { className: "meta" }, `Granted by ${u.granted_by_sub} on ${fmtDay(u.created_at)}`),
            );
            if (canRemove) {
                card.appendChild(h("div", { className: "btn-row" },
                    h("button", { className: "btn danger tiny",
                        onclick: async () => {
                            if (!confirm(`Remove ${u.user_sub}? All their kubeconfigs on this cluster will also be revoked.`)) return;
                            try {
                                await api(`/api/clusters/${slug}/users/${encodeURIComponent(u.user_sub)}`, { method: "DELETE" });
                                renderClusterUsers(slug);
                            } catch (err) { showAlert(err.message); }
                        }}, "Remove"),
                ));
            }
            app.appendChild(card);
        }

        app.appendChild(slbl("Add user"));
        const form = h("form", { className: "card",
            onsubmit: async (e) => {
                e.preventDefault();
                const user_sub = e.target.querySelector('[name="user_sub"]').value.trim();
                const role = e.target.querySelector('[name="role"]').value;
                try {
                    await api(`/api/clusters/${slug}/users`, { method: "POST", body: JSON.stringify({ user_sub, role }) });
                    renderClusterUsers(slug);
                } catch (err) { showAlert(err.message); }
            }},
            h("div", { className: "meta" },
                h("label", { style: "display:block;margin-bottom:6px;font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:1px" }, "OIDC subject"),
                h("input", { name: "user_sub", required: true, placeholder: "user@idp",
                    style: "width:100%;padding:10px;border:1px solid var(--line);border-radius:6px;font-family:inherit" }),
                h("label", { style: "display:block;margin-top:10px;margin-bottom:6px;font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:1px" }, "Role"),
                h("select", { name: "role",
                    style: "width:100%;padding:10px;border:1px solid var(--line);border-radius:6px;font-family:inherit;background:var(--surface)" },
                    h("option", { value: "user" }, "user"),
                    isSunetAdmin ? h("option", { value: "customer_admin" }, "customer_admin (SUNET admin only)") : null,
                ),
                h("div", { className: "btn-row" },
                    h("button", { type: "submit", className: "btn primary sm" }, "Grant access"),
                ),
            ),
        );
        app.appendChild(form);
    } catch (e) { showAlert(e.message); }
}

// ========== Admin landing + clusters + cluster requests + setup guide ==========

function renderAdmin() {
    clear(app); app.className = "page";
    app.appendChild(bc({ label: "Admin" }));
    app.appendChild(phead({
        eyebrow: "Operator",
        title: "Admin",
        lead: "SUNET-only management surfaces.",
    }));
    const tile = (href, title, desc) => h("a", { className: "card link", href },
        h("div", { className: "card-head" }, h("h3", {}, title)),
        h("div", { className: "meta" }, desc),
    );
    app.appendChild(tile("#/admin/customers", "Customers & Contracts",
        "Create customer organisations, contracts, and grant user access to contracts."));
    app.appendChild(tile("#/admin/clusters", "Tenant Clusters",
        "Register Kubernetes clusters, mark as provisioned, manage admin access. Includes the bootstrap setup guide."));
    app.appendChild(tile("#/admin/cluster-requests", "Cluster Change Requests",
        "Review and apply customer-admin requests for addons, resizes, and backup enablement."));
    app.appendChild(tile("#/admin/billing", "Billing Jobs",
        "All scheduled billing exports across the platform — read-only view with manual-run support."));
    app.appendChild(tile("#/admin/pricing", "Pricing",
        "Per-resource unit prices, per-contract overrides, and rebates. Includes synthetic cluster fees."));
    app.appendChild(tile("#/admin/pricing/docs", "Pricing Docs",
        "Reference: how unit prices and conversion factors map to billed line items."));
}

async function renderAdminClusters() {
    clear(app); app.className = "page";
    app.appendChild(bc({ label: "Admin", hash: "/admin" }, { label: "Clusters" }));
    app.appendChild(phead({
        eyebrow: "Operator",
        title: "Tenant clusters",
        lead: "All registered tenant clusters across customers.",
        actions: [
            h("a", { className: "btn primary sm", href: "#/admin/clusters/new" }, svgPlus(), "New cluster"),
            h("a", { className: "btn ghost sm", href: "#/admin/clusters/help" }, "Setup guide"),
        ],
    }));
    try {
        const clusters = await api("/api/admin/clusters");
        app.appendChild(slbl("Clusters", clusters.length));
        if (!clusters.length) {
            app.appendChild(emptyState("No clusters yet."));
            return;
        }
        for (const c of clusters) {
            app.appendChild(h("a", { className: "card link", href: `#/admin/clusters/${encodeURIComponent(c.slug)}` },
                h("div", { className: "card-head" },
                    h("h3", {}, c.name),
                    c.provisioned_at ? badge("provisioned", "ok") : badge("pending", "warn"),
                ),
                h("div", { className: "meta" }, `${c.size_label} — ${c.total_servers} servers · contract ${c.contract_number}`),
                h("div", { className: "meta mono" }, c.api_url),
            ));
        }
    } catch (e) { showAlert(e.message); }
}

async function renderAdminCreateCluster() {
    clear(app); app.className = "page narrow-form";
    app.appendChild(bc(
        { label: "Admin", hash: "/admin" },
        { label: "Clusters", hash: "/admin/clusters" },
        { label: "New" },
    ));
    app.appendChild(phead({
        eyebrow: "Operator",
        title: "Register tenant cluster",
        lead: "After kubespray finishes, register the cluster here so the portal can mediate kubeconfig issuance and billing.",
    }));

    let contracts = [];
    let customersById = {};
    try {
        contracts = await api("/api/admin/contracts");
        const customers = await api("/api/admin/customers");
        customersById = Object.fromEntries((customers || []).map(c => [c.id, c]));
    } catch (e) { showAlert(e.message); return; }

    if (!contracts.length) {
        app.appendChild(emptyState("No contracts exist yet. Create a customer + contract first under Admin → Customers."));
        app.appendChild(h("div", { className: "btn-row" },
            h("a", { className: "btn ghost sm", href: "#/admin/customers" }, "Go to Customers"),
        ));
        return;
    }

    const inputCss = "width:100%;padding:10px;border:1px solid var(--line);border-radius:6px;font-family:inherit;background:var(--surface)";
    const labelCss = "display:block;margin-top:14px;margin-bottom:6px;font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:1px";

    const form = h("form", { className: "card",
        onsubmit: async (e) => {
            e.preventDefault();
            const data = Object.fromEntries(new FormData(e.target).entries());
            data.worker_groups = parseInt(data.worker_groups, 10) || 1;
            try {
                const created = await api("/api/admin/clusters", {
                    method: "POST", body: JSON.stringify(data),
                });
                navigate(`/admin/clusters/${encodeURIComponent(created.slug)}`);
            } catch (err) { showAlert(err.message); }
        }},
        h("label", { style: labelCss }, "Contract"),
        h("select", { name: "contract_number", required: true, style: inputCss },
            h("option", { value: "" }, "— select contract —"),
            ...contracts.map(c => {
                const cust = customersById[c.customer_id];
                const lbl = cust ? `${c.contract_number} — ${cust.name} (${cust.domain})` : c.contract_number;
                return h("option", { value: c.contract_number }, lbl);
            }),
        ),
        h("label", { style: labelCss }, "Display name"),
        h("input", { name: "name", required: true, placeholder: "Acme production cluster", style: inputCss }),
        h("label", { style: labelCss }, "Slug (used in OpenBao mount path & cert O)"),
        h("input", { name: "slug", required: true, pattern: "[a-z0-9]([a-z0-9-]*[a-z0-9])?", maxlength: "64", placeholder: "acme-prod", style: inputCss }),
        h("label", { style: labelCss }, "API URL"),
        h("input", { name: "api_url", required: true, placeholder: "https://k8s.acme.example.org:6443", style: inputCss }),
        h("label", { style: labelCss }, "CA bundle (PEM)"),
        h("div", { className: "meta", style: "margin-top:-2px" },
            "On a control-plane node: ", h("code", {}, "cat /etc/kubernetes/pki/ca.crt"),
            ". Or from an admin kubeconfig: ", h("code", {}, "kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d"), "."),
        h("textarea", { name: "ca_bundle", required: true, placeholder: "-----BEGIN CERTIFICATE-----\n...",
            style: inputCss + ";min-height:130px;font-family:var(--mono);font-size:12px" }),
        h("div", { className: "meta", style: "margin-top:6px" },
            "OpenBao mount will be derived as ", h("code", {}, "kubernetes/<slug>"), "."),
        h("label", { style: labelCss }, "OpenBao role"),
        h("input", { name: "openbao_role", value: "argocd-rbac-manager", style: inputCss }),
        h("label", { style: labelCss }, "ArgoCD Role name (in argocd ns)"),
        h("input", { name: "argocd_role_name", value: "argocd-tenant", style: inputCss }),
        h("label", { style: labelCss }, "ArgoCD namespace"),
        h("input", { name: "argocd_namespace", value: "argocd", style: inputCss }),
        h("label", { style: labelCss }, "Worker groups (3 workers per group)"),
        h("input", { name: "worker_groups", type: "number", min: "1", value: "1", required: true, style: inputCss }),
        h("div", { className: "btn-row" },
            h("a", { className: "btn ghost sm", href: "#/admin/clusters" }, "Cancel"),
            h("button", { type: "submit", className: "btn primary sm" }, "Create cluster"),
        ),
    );
    app.appendChild(form);
}

async function renderAdminClusterDetail(slug) {
    clear(app); app.className = "page";
    app.appendChild(bc(
        { label: "Admin", hash: "/admin" },
        { label: "Clusters", hash: "/admin/clusters" },
        { label: slug },
    ));
    try {
        const c = await api(`/api/admin/clusters/${slug}`);
        app.appendChild(phead({
            eyebrow: `Cluster · ${c.size_label}`,
            title: c.name,
            lead: c.provisioned_at ? "Provisioned and live." : "Not yet provisioned.",
            actions: [
                !c.provisioned_at ? h("button", { className: "btn primary sm",
                    onclick: async () => {
                        if (!confirm("Mark this cluster as provisioned? This starts billing for the initial setup fee in the next billing run.")) return;
                        try {
                            await api(`/api/admin/clusters/${slug}/provision`, { method: "POST" });
                            renderAdminClusterDetail(slug);
                        } catch (err) { showAlert(err.message); }
                    }}, "Mark provisioned") : null,
                h("a", { className: "btn ghost sm", href: `#/clusters/${encodeURIComponent(slug)}` }, "Open as user"),
                h("button", { className: "btn danger sm",
                    onclick: async () => {
                        if (!confirm(`Delete cluster ${c.name}? This deletes the management/backup OpenStack projects and revokes all credentials. Cannot be undone.`)) return;
                        try {
                            await api(`/api/admin/clusters/${slug}`, { method: "DELETE" });
                            navigate("/admin/clusters");
                        } catch (err) { showAlert(err.message); }
                    }}, "Delete cluster"),
            ].filter(Boolean),
        }));

        app.appendChild(h("div", { className: "slbl first" }, "Cluster"));
        app.appendChild(kv(
            kvRowMono("Slug", c.slug),
            kvRow("Size", `${c.size_label} (${c.total_servers} servers)`),
            kvRowMono("API", c.api_url),
            kvRow("Contract", c.contract_number),
            kvRow("Provisioned", c.provisioned_at ? fmtDay(c.provisioned_at) : "(not yet)"),
            kvRow("Management project", c.management_project_resource_name || "—"),
            kvRow("Backup project", c.backup_project_resource_name || "—"),
        ));
    } catch (e) { showAlert(e.message); }
}

async function renderAdminClusterRequests() {
    clear(app); app.className = "page";
    app.appendChild(bc(
        { label: "Admin", hash: "/admin" },
        { label: "Cluster requests" },
    ));
    app.appendChild(phead({
        eyebrow: "Operator",
        title: "Pending cluster requests",
        lead: "Review and apply customer-admin requests across clusters. Apply triggers state changes (addon enable, resize bump, backup project create) and the next billing run reflects them.",
    }));
    try {
        const pending = await api("/api/admin/cluster-requests?status=pending");
        app.appendChild(slbl("Pending", pending.length));
        if (!pending.length) {
            app.appendChild(emptyState("No pending requests."));
        }
        for (const r of pending) {
            const card = h("div", { className: "card" },
                h("div", { className: "card-head" },
                    h("h3", {}, `${formatRequestSummary(r)} — ${r.cluster_slug}`),
                    statusBadge(r.status),
                ),
                h("div", { className: "meta" }, `Requested by ${r.requested_by_sub} on ${fmtDay(r.requested_at)}`),
                h("textarea", { name: `note-${r.id}`, placeholder: "Optional note",
                    style: "width:100%;height:60px;margin-top:10px;padding:10px;border:1px solid var(--line);border-radius:6px;font-family:inherit" }),
                h("div", { className: "btn-row" },
                    h("button", { className: "btn primary sm",
                        onclick: async () => {
                            const note = card.querySelector(`[name="note-${r.id}"]`).value.trim();
                            try {
                                await api(`/api/admin/cluster-requests/${r.id}/apply`, {
                                    method: "POST", body: JSON.stringify({ note: note || null }),
                                });
                                renderAdminClusterRequests();
                            } catch (err) { showAlert(err.message); }
                        }}, "Apply"),
                    h("button", { className: "btn danger sm",
                        onclick: async () => {
                            const note = card.querySelector(`[name="note-${r.id}"]`).value.trim();
                            try {
                                await api(`/api/admin/cluster-requests/${r.id}/deny`, {
                                    method: "POST", body: JSON.stringify({ note: note || null }),
                                });
                                renderAdminClusterRequests();
                            } catch (err) { showAlert(err.message); }
                        }}, "Deny"),
                ),
            );
            app.appendChild(card);
        }

        const all = await api("/api/admin/cluster-requests");
        const recent = all.filter(x => x.status !== "pending").slice(0, 20);
        app.appendChild(slbl("Recent applied/denied", recent.length));
        for (const r of recent) {
            app.appendChild(h("div", { className: "card" },
                h("div", { className: "card-head" },
                    h("h3", {}, `${formatRequestSummary(r)} — ${r.cluster_slug}`),
                    statusBadge(r.status),
                ),
                h("div", { className: "meta" }, `${r.status === "applied" ? "Applied" : "Denied"} by ${r.applied_by_sub} on ${fmtDay(r.applied_at)}`),
                r.note ? h("div", { className: "meta" }, `Note: ${r.note}`) : null,
            ));
        }
    } catch (e) { showAlert(e.message); }
}

function renderClusterSetupHelp() {
    clear(app); app.className = "page";
    app.appendChild(bc(
        { label: "Admin", hash: "/admin" },
        { label: "Clusters", hash: "/admin/clusters" },
        { label: "Setup guide" },
    ));
    app.appendChild(phead({
        eyebrow: "Operator",
        title: "Tenant cluster setup guide",
        lead: "Walks through every manual step needed to onboard a fresh tenant cluster. Run prerequisite steps once for the platform, then the per-cluster steps for each cluster you onboard.",
    }));

    const codeBlock = (txt) => h("pre", { className: "help-code" }, h("code", {}, txt));
    const sect = (text) => h("div", { className: "slbl" }, text);
    const sub = (text) => h("h4", { style: "margin-top:18px;margin-bottom:6px;font-family:var(--mono);font-size:12px;text-transform:uppercase;letter-spacing:1px" }, text);

    app.appendChild(sect("Prerequisites (one-time, platform-wide)"));
    app.appendChild(h("p", { className: "hint" },
        "Done once on the platform side. Skip if these are already in place from a previous cluster onboarding."));

    app.appendChild(sub("1. OpenBao Kubernetes auth method"));
    app.appendChild(h("p", {},
        "If ", h("code", {}, "auth/kubernetes/"), " is not yet enabled in OpenBao, enable it and point it at the platform K8s API:"));
    app.appendChild(codeBlock(
        "bao auth enable kubernetes\n" +
        "bao write auth/kubernetes/config \\\n" +
        "    kubernetes_host=https://kubernetes.default.svc \\\n" +
        "    kubernetes_ca_cert=@/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    ));

    app.appendChild(sub("2. Portal Vault policy + role"));
    app.appendChild(h("p", {},
        "Allows the portal pod to mint short-lived ephemeral SA tokens from any per-tenant secrets engine."));
    app.appendChild(codeBlock(
        "cat > /tmp/customer-portal.hcl <<'EOF'\n" +
        "path \"kubernetes/+/creds/argocd-rbac-manager\" {\n" +
        "  capabilities = [\"update\"]\n" +
        "}\n" +
        "EOF\n" +
        "bao policy write customer-portal /tmp/customer-portal.hcl\n\n" +
        "bao write auth/kubernetes/role/customer-portal \\\n" +
        "    bound_service_account_names=customer-portal \\\n" +
        "    bound_service_account_namespaces=customer-portal \\\n" +
        "    policies=customer-portal \\\n" +
        "    ttl=1h"
    ));
    app.appendChild(h("p", { className: "hint" },
        "The capability is ", h("code", {}, "update"),
        " (not ", h("code", {}, "read"),
        "): the kubernetes secrets engine generates credentials via POST, which Vault/OpenBao maps to ",
        h("code", {}, "update"), " — using ", h("code", {}, "read"),
        " yields a 403 permission-denied at credential mint time."));

    app.appendChild(sub("3. Portal records"));
    app.appendChild(h("p", {},
        "The cluster will be tied to a contract under a customer. Create both first under ",
        h("a", { className: "link", href: "#/admin/customers" }, "Admin → Customers"),
        " if they don't exist yet."));

    app.appendChild(sect("Per-cluster bootstrap"));
    app.appendChild(h("p", { className: "hint" },
        "Run these every time you onboard a new tenant cluster. ",
        h("code", {}, "<slug>"), " must match the slug entered in the Register Tenant Cluster form."));

    app.appendChild(sub("1. Provision the cluster"));
    app.appendChild(h("p", {},
        "Spin up the K8s cluster with kubespray and install ArgoCD into the ",
        h("code", {}, "argocd"), " namespace as usual. This guide picks up after that."));

    app.appendChild(sub("2. Apply RBAC inside the tenant cluster"));
    app.appendChild(h("p", {},
        "Creates the Role customer kubeconfigs are bound to (",
        h("code", {}, "argocd-tenant"), " in the ",
        h("code", {}, "argocd"), " namespace), and the SA + ClusterRole that OpenBao authenticates as to mint per-issuance RoleBindings and CSRs."));
    app.appendChild(codeBlock(
        "cat > /tmp/tenant-bootstrap.yaml <<'EOF'\n" +
        "---\n" +
        "apiVersion: rbac.authorization.k8s.io/v1\n" +
        "kind: ClusterRole\n" +
        "metadata:\n" +
        "  name: argocd-rbac-manager\n" +
        "rules:\n" +
        "  - apiGroups: [\"rbac.authorization.k8s.io\"]\n" +
        "    resources: [\"rolebindings\"]\n" +
        "    verbs: [\"create\", \"get\", \"list\", \"delete\"]\n" +
        "  - apiGroups: [\"rbac.authorization.k8s.io\"]\n" +
        "    resources: [\"roles\"]\n" +
        "    resourceNames: [\"argocd-tenant\"]\n" +
        "    verbs: [\"bind\"]\n" +
        "  - apiGroups: [\"certificates.k8s.io\"]\n" +
        "    resources: [\"certificatesigningrequests\"]\n" +
        "    verbs: [\"create\", \"get\", \"delete\"]\n" +
        "  - apiGroups: [\"certificates.k8s.io\"]\n" +
        "    resources: [\"certificatesigningrequests/approval\"]\n" +
        "    verbs: [\"update\"]\n" +
        "  - apiGroups: [\"certificates.k8s.io\"]\n" +
        "    resources: [\"signers\"]\n" +
        "    resourceNames: [\"kubernetes.io/kube-apiserver-client\"]\n" +
        "    verbs: [\"approve\"]\n" +
        "  - apiGroups: [\"\"]\n" +
        "    resources: [\"serviceaccounts\"]\n" +
        "    resourceNames: [\"openbao-rbac-manager\"]\n" +
        "    verbs: [\"get\"]\n" +
        "  - apiGroups: [\"\"]\n" +
        "    resources: [\"serviceaccounts/token\"]\n" +
        "    resourceNames: [\"openbao-rbac-manager\"]\n" +
        "    verbs: [\"create\"]\n" +
        "---\n" +
        "apiVersion: rbac.authorization.k8s.io/v1\n" +
        "kind: Role\n" +
        "metadata:\n" +
        "  name: argocd-tenant\n" +
        "  namespace: argocd\n" +
        "rules:\n" +
        "  - apiGroups: [\"argoproj.io\"]\n" +
        "    resources: [\"applications\", \"appprojects\", \"applicationsets\"]\n" +
        "    verbs: [\"get\", \"list\", \"watch\", \"create\", \"update\", \"patch\", \"delete\"]\n" +
        "  - apiGroups: [\"\"]\n" +
        "    resources: [\"configmaps\", \"secrets\"]\n" +
        "    verbs: [\"get\", \"list\", \"watch\", \"create\", \"update\", \"patch\", \"delete\"]\n" +
        "  - apiGroups: [\"\"]\n" +
        "    resources: [\"events\", \"pods\", \"pods/log\"]\n" +
        "    verbs: [\"get\", \"list\", \"watch\"]\n" +
        "---\n" +
        "apiVersion: v1\n" +
        "kind: ServiceAccount\n" +
        "metadata:\n" +
        "  name: openbao-rbac-manager\n" +
        "  namespace: kube-system\n" +
        "---\n" +
        "apiVersion: rbac.authorization.k8s.io/v1\n" +
        "kind: ClusterRoleBinding\n" +
        "metadata:\n" +
        "  name: openbao-rbac-manager\n" +
        "roleRef:\n" +
        "  apiGroup: rbac.authorization.k8s.io\n" +
        "  kind: ClusterRole\n" +
        "  name: argocd-rbac-manager\n" +
        "subjects:\n" +
        "  - kind: ServiceAccount\n" +
        "    name: openbao-rbac-manager\n" +
        "    namespace: kube-system\n" +
        "---\n" +
        "apiVersion: v1\n" +
        "kind: Secret\n" +
        "metadata:\n" +
        "  name: openbao-rbac-manager-token\n" +
        "  namespace: kube-system\n" +
        "  annotations:\n" +
        "    kubernetes.io/service-account.name: openbao-rbac-manager\n" +
        "type: kubernetes.io/service-account-token\n" +
        "EOF\n" +
        "kubectl --context <tenant-cluster> apply -f /tmp/tenant-bootstrap.yaml"
    ));

    app.appendChild(sub("3. Configure OpenBao secrets engine for the cluster"));
    app.appendChild(codeBlock(
        "JWT=$(kubectl --context <tenant-cluster> -n kube-system get secret openbao-rbac-manager-token \\\n" +
        "        -o jsonpath='{.data.token}' | base64 -d)\n" +
        "CA=$(kubectl --context <tenant-cluster> config view --raw --minify \\\n" +
        "        -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d)\n" +
        "APIURL=$(kubectl --context <tenant-cluster> config view --raw --minify \\\n" +
        "        -o jsonpath='{.clusters[0].cluster.server}')\n\n" +
        "bao secrets enable -path=kubernetes/<slug> kubernetes\n" +
        "bao write kubernetes/<slug>/config \\\n" +
        "    kubernetes_host=\"$APIURL\" \\\n" +
        "    kubernetes_ca_cert=\"$CA\" \\\n" +
        "    service_account_jwt=\"$JWT\"\n" +
        "bao write kubernetes/<slug>/roles/argocd-rbac-manager \\\n" +
        "    allowed_kubernetes_namespaces=kube-system \\\n" +
        "    service_account_name=openbao-rbac-manager \\\n" +
        "    token_default_ttl=600s \\\n" +
        "    token_max_ttl=600s"
    ));
    app.appendChild(h("p", { className: "hint" },
        "Single ", h("code", {}, "allowed_kubernetes_namespaces"),
        " (only ", h("code", {}, "kube-system"),
        ") because that's where ", h("code", {}, "openbao-rbac-manager"),
        " lives. TTL 600s is the K8s TokenRequest minimum; the portal uses the token only briefly per issuance."));

    app.appendChild(sub("4. Register the cluster in the portal"));
    app.appendChild(h("p", {},
        "Open ", h("a", { className: "link", href: "#/admin/clusters/new" }, "Admin → Clusters → + New cluster"),
        ". Slug must match ", h("code", {}, "<slug>"), " from step 3."));

    app.appendChild(sub("5. Mark the cluster provisioned"));
    app.appendChild(h("p", {},
        "Click ", h("strong", {}, "Mark provisioned"), " on the cluster's admin detail page. Sets ",
        h("code", {}, "provisioned_at"), " and unlocks credential issuance for users + initial setup-fee billing."));

    app.appendChild(sub("6. Grant the first customer admin"));
    app.appendChild(h("p", {},
        "Open the cluster's user-facing detail page, click ",
        h("strong", {}, "Manage users"),
        ", add a user with role ", h("code", {}, "customer_admin"),
        ". They can then add their team."));
}

// ---------- Init ----------

route();
