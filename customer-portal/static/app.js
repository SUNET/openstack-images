/* SUNET Cloud Portal — vanilla JS SPA with hash routing */

const $ = (sel) => document.querySelector(sel);
const app = $("#app");
const nav = $("#nav");

let currentUser = null;

// --- Router ---

function navigate(hash) {
    if (location.hash === "#" + hash) route();
    else location.hash = hash;
}

function currentRoute() { return location.hash.replace(/^#\/?/, ""); }

async function route() {
    if (!currentUser) {
        try {
            currentUser = await api("/api/me");
            if (!currentUser) { renderLogin(); return; }
            renderNav();
        } catch {
            renderLogin();
            return;
        }
    }

    const path = currentRoute();
    const parts = path.split("/").filter(Boolean);

    // Customer routes
    if (parts[0] === "contracts" && parts[2] === "projects" && parts[3] === "new")
        return renderCreateProject(decodeURIComponent(parts[1]));
    if (parts[0] === "contracts" && parts[2] === "projects" && parts[3] === "edit" && parts[4])
        return renderEditProject(decodeURIComponent(parts[1]), decodeURIComponent(parts[4]));
    if (parts[0] === "contracts" && parts[2] === "projects" && parts[3])
        return renderProjectDetail(decodeURIComponent(parts[1]), decodeURIComponent(parts[3]));
    if (parts[0] === "contracts" && parts[2] === "projects")
        return renderContractProjects(decodeURIComponent(parts[1]));
    if (parts[0] === "contracts" || !path)
        return renderContracts();

    // Cluster routes (member-facing)
    if (parts[0] === "clusters" && parts[1] && parts[2] === "users")
        return renderClusterUsers(decodeURIComponent(parts[1]));
    if (parts[0] === "clusters" && parts[1])
        return renderClusterDetail(decodeURIComponent(parts[1]));
    if (parts[0] === "clusters")
        return renderClusters();

    // Billing routes
    if (parts[0] === "billing" && parts[1] === "new")
        return renderCreateBillingJob();
    if (parts[0] === "billing" && parts[1] && parts[2] === "edit")
        return renderEditBillingJob(parts[1]);
    if (parts[0] === "billing" && parts[1])
        return renderBillingJobDetail(parts[1]);
    if (parts[0] === "billing")
        return renderBillingJobs();

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

    renderContracts();
}

window.addEventListener("hashchange", route);

// --- API helpers ---

async function api(path, opts = {}) {
    const resp = await fetch(path, {
        headers: { "Content-Type": "application/json", ...opts.headers },
        ...opts,
    });
    if (resp.status === 401) { currentUser = null; renderLogin(); return null; }
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        throw new Error(err.detail || "Request failed");
    }
    if (resp.status === 204) return null;
    return resp.json();
}

// --- Rendering helpers ---

function h(tag, attrs = {}, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
        else if (k === "className") el.className = v;
        else if (k === "htmlFor") el.setAttribute("for", v);
        else el.setAttribute(k, v);
    }
    for (const child of children) {
        if (typeof child === "string") el.appendChild(document.createTextNode(child));
        else if (child) el.appendChild(child);
    }
    return el;
}

function clear(el) { el.innerHTML = ""; return el; }

function breadcrumbs(...items) {
    const bc = h("nav", { className: "breadcrumbs" });
    items.forEach((item, i) => {
        if (i > 0) bc.appendChild(h("span", { className: "sep" }, "/"));
        if (i < items.length - 1 && item.hash)
            bc.appendChild(h("a", { href: "#/" + item.hash }, item.label));
        else
            bc.appendChild(h("span", { className: "current" }, item.label));
    });
    return bc;
}

function phaseBadge(phase) {
    if (!phase) return h("span", { className: "badge badge-pending" }, "Unknown");
    if (phase === "Ready") return h("span", { className: "badge badge-ready" }, "Ready");
    if (phase.includes("Error")) return h("span", { className: "badge badge-error" }, phase);
    return h("span", { className: "badge badge-pending" }, phase);
}

function showAlert(msg, type = "error") {
    const existing = app.querySelector(".alert");
    if (existing) existing.remove();
    app.prepend(h("div", { className: `alert alert-${type}` }, msg));
}

// --- Navigation ---

function renderNav() {
    clear(nav);
    if (!currentUser) return;
    nav.appendChild(h("a", { href: "#/contracts" }, "My Contracts"));
    nav.appendChild(h("a", { href: "#/billing" }, "Billing"));
    if (currentUser.is_admin) {
        nav.appendChild(h("a", { href: "#/admin" }, "Admin"));
    }
    nav.appendChild(h("a", { href: "#", className: "nav-user" }, currentUser.sub));
    nav.appendChild(h("a", { href: "/auth/logout", className: "nav-logout" }, "Sign out"));
}

// --- Login ---

function renderLogin() {
    renderNav();
    clear(app).appendChild(
        h("div", { className: "login-prompt" },
            h("h2", {}, "SUNET Cloud Portal"),
            h("p", {}, "Sign in to manage your cloud projects."),
            h("a", { href: "/auth/login", className: "btn btn-primary" }, "Sign in with SSO"),
        )
    );
}

// ========== CUSTOMER VIEWS ==========

async function renderContracts() {
    clear(app);
    app.appendChild(breadcrumbs({ label: "My Contracts" }));
    app.appendChild(h("h2", {}, "My Contracts"));
    app.appendChild(h("p", { className: "page-desc" }, "Select a contract to view and manage its projects."));
    try {
        const user = await api("/api/me");
        if (!user) return;
        currentUser = user;
        renderNav();
        if (!user.contracts.length) {
            app.appendChild(h("p", { className: "empty" }, "You don't have access to any contracts yet. Ask an administrator to grant you access."));
            return;
        }
        for (const c of user.contracts) {
            const cn = encodeURIComponent(c.contract_number);
            app.appendChild(
                h("a", { href: `#/contracts/${cn}/projects`, className: "card card-clickable", style: "display:block;text-decoration:none;color:inherit" },
                    h("div", { className: "card-header" },
                        h("h3", {}, c.contract_number),
                        h("span", { className: "badge badge-neutral" }, c.customer.domain),
                    ),
                    h("p", { className: "meta" }, c.customer.name + (c.description ? " — " + c.description : "")),
                )
            );
        }
    } catch (e) { showAlert(e.message); }
}

async function renderContractProjects(contractNumber) {
    clear(app);
    const contractInfo = currentUser.contracts.find(c => c.contract_number === contractNumber);
    const customerName = contractInfo ? contractInfo.customer.name : "";
    const cn = encodeURIComponent(contractNumber);

    app.appendChild(breadcrumbs(
        { label: "My Contracts", hash: "contracts" },
        { label: contractNumber },
    ));
    app.appendChild(h("h2", {}, contractNumber));
    app.appendChild(h("p", { className: "page-desc" }, customerName));

    // --- Projects section ---
    app.appendChild(h("h3", { style: "margin-top:24px;margin-bottom:8px" }, "Projects"));
    app.appendChild(h("a", { href: `#/contracts/${cn}/projects/new`, className: "btn btn-primary btn-small", style: "display:inline-block;margin-bottom:16px;text-decoration:none" }, "+ New Project"));

    try {
        const projects = await api(`/api/contracts/${contractNumber}/projects`);
        if (!projects.length) {
            app.appendChild(h("p", { className: "empty" }, "No projects yet. Create one to get started."));
        } else {
            for (const p of projects) {
                const rn = encodeURIComponent(p.resource_name);
                const headerChildren = [
                    h("h3", {}, p.name),
                    phaseBadge(p.phase),
                ];
                if (p.managed) {
                    headerChildren.push(
                        h("span", { className: "badge badge-managed", title: "Managed by SUNET" }, "managed-by-sunet"),
                    );
                }
                app.appendChild(
                    h("a", { href: `#/contracts/${cn}/projects/${rn}`, className: "card card-clickable", style: "display:block;text-decoration:none;color:inherit" },
                        h("div", { className: "card-header" }, ...headerChildren),
                        p.description ? h("p", { className: "meta" }, p.description) : null,
                        h("p", { className: "meta" }, "Users: " + (p.users.length ? p.users.join(", ") : "(SUNET-managed)")),
                    )
                );
            }
        }

        // --- Clusters section ---
        app.appendChild(h("h3", { style: "margin-top:32px;margin-bottom:8px" }, "Clusters"));
        const allClusters = await api("/api/clusters");
        const clusters = allClusters.filter(c => c.contract_number === contractNumber);
        if (!clusters.length) {
            app.appendChild(h("p", { className: "empty" }, "No clusters on this contract. Clusters are provisioned by SUNET — contact ops to request one."));
        } else {
            for (const c of clusters) {
                app.appendChild(
                    h("a", { href: `#/clusters/${encodeURIComponent(c.slug)}`, className: "card card-clickable", style: "display:block;text-decoration:none;color:inherit" },
                        h("div", { className: "card-header" },
                            h("h3", {}, c.name),
                            c.provisioned_at
                                ? h("span", { className: "badge badge-ready" }, "provisioned")
                                : h("span", { className: "badge badge-pending" }, "pending"),
                        ),
                        h("p", { className: "meta" }, `${c.size_label} — ${c.total_servers} servers (3 controllers + ${3 * c.worker_groups} workers)`),
                        h("p", { className: "meta" }, `Your role: ${c.caller_role || "?"}` + (c.active_addons.length ? " · Addons: " + c.active_addons.join(", ") : "")),
                    )
                );
            }
        }
    } catch (e) { showAlert(e.message); }
}

async function renderProjectDetail(contractNumber, resourceName) {
    clear(app);
    const cn = encodeURIComponent(contractNumber);
    const rn = encodeURIComponent(resourceName);

    app.appendChild(breadcrumbs(
        { label: "My Contracts", hash: "contracts" },
        { label: contractNumber, hash: `contracts/${cn}/projects` },
        { label: resourceName },
    ));

    try {
        const p = await api(`/api/contracts/${contractNumber}/projects/${resourceName}`);
        const titleRow = [h("h2", {}, p.name)];
        if (p.managed) {
            titleRow.push(
                h("span", {
                    className: "badge badge-managed",
                    style: "margin-left:8px;vertical-align:middle",
                    title: "Managed by SUNET — read-only for customer admins"
                }, "managed-by-sunet")
            );
        }
        app.appendChild(h("div", { style: "display:flex;align-items:center;gap:8px;margin-bottom:8px" }, ...titleRow));
        app.appendChild(h("div", { className: "card", style: "margin-bottom:16px" },
            h("div", { className: "card-header" },
                h("div", { className: "section-label", style: "margin:0" }, "Status"),
                phaseBadge(p.phase),
            ),
        ));

        app.appendChild(h("div", { className: "card" },
            h("div", { className: "section-label", style: "margin-top:0" }, "Description"),
            h("p", {}, p.description || "(none)"),
            h("div", { className: "section-label" }, "Users"),
            ...p.users.map(u => h("p", {}, u)),
            p.users.length === 0 ? h("p", { className: "meta" }, p.managed ? "(SUNET-managed)" : "(none)") : null,
            h("div", { className: "section-label" }, "Contract"),
            h("p", {}, p.contract_number),
        ));

        // Customer admins on managed projects can view but not edit/delete.
        const canMutate = !p.managed || (currentUser && currentUser.is_admin);
        if (canMutate) {
            app.appendChild(h("div", { className: "btn-row", style: "margin-top:16px" },
                h("a", { href: `#/contracts/${cn}/projects/edit/${rn}`, className: "btn btn-primary btn-small", style: "text-decoration:none" }, "Edit Project"),
                h("button", { className: "btn btn-danger", onclick: async () => {
                    if (confirm(`Delete project ${p.name}? This will remove the OpenStack project and all its resources. This cannot be undone.`)) {
                        try {
                            await api(`/api/contracts/${contractNumber}/projects/${resourceName}`, { method: "DELETE" });
                            navigate(`/contracts/${cn}/projects`);
                        } catch (err) { showAlert(err.message); }
                    }
                }}, "Delete Project"),
            ));
        } else {
            app.appendChild(h("p", { className: "meta", style: "margin-top:16px" },
                "This project is SUNET-managed and read-only. Use the cluster page to request changes."));
        }
    } catch (e) { showAlert(e.message); }
}

function renderCreateProject(contractNumber) {
    clear(app);
    const contractInfo = currentUser.contracts.find(c => c.contract_number === contractNumber);
    const customerDomain = contractInfo ? contractInfo.customer.domain : "";
    const cn = encodeURIComponent(contractNumber);

    app.appendChild(breadcrumbs(
        { label: "My Contracts", hash: "contracts" },
        { label: contractNumber, hash: `contracts/${cn}/projects` },
        { label: "New Project" },
    ));
    app.appendChild(h("h2", {}, "New Project"));

    const form = h("form", { className: "form-card", onsubmit: async (e) => {
        e.preventDefault();
        const name = form.querySelector('[name="name"]').value.trim();
        const description = form.querySelector('[name="description"]').value.trim();
        const usersRaw = form.querySelector('[name="users"]').value.trim();
        const users = usersRaw ? usersRaw.split("\n").map(u => u.trim()).filter(Boolean) : [];
        try {
            await api(`/api/contracts/${contractNumber}/projects`, {
                method: "POST", body: JSON.stringify({ name, description, users }),
            });
            navigate(`/contracts/${cn}/projects`);
        } catch (err) { showAlert(err.message); }
    }},
        h("label", {}, "Project name"),
        h("div", { className: "input-with-suffix" },
            h("input", { name: "name", required: "true", maxlength: "64", placeholder: "my-project", pattern: "[a-z0-9]([a-z0-9-]*[a-z0-9])?" }),
            customerDomain ? h("span", { className: "input-suffix" }, "." + customerDomain) : null,
        ),
        h("label", {}, "Description"),
        h("input", { name: "description", placeholder: "Optional description" }),
        h("label", {}, "Users (one identifier per line)"),
        h("textarea", { name: "users", placeholder: "user1@idp\nuser2@idp" }),
        h("div", { className: "btn-row" },
            h("a", { href: `#/contracts/${cn}/projects`, className: "btn btn-secondary btn-small", style: "text-decoration:none" }, "Cancel"),
            h("button", { type: "submit", className: "btn btn-primary btn-small" }, "Create Project"),
        ),
    );
    app.appendChild(form);
}

async function renderEditProject(contractNumber, resourceName) {
    clear(app);
    const cn = encodeURIComponent(contractNumber);
    const rn = encodeURIComponent(resourceName);

    app.appendChild(breadcrumbs(
        { label: "My Contracts", hash: "contracts" },
        { label: contractNumber, hash: `contracts/${cn}/projects` },
        { label: resourceName, hash: `contracts/${cn}/projects/${rn}` },
        { label: "Edit" },
    ));
    app.appendChild(h("h2", {}, "Edit Project"));

    try {
        const p = await api(`/api/contracts/${contractNumber}/projects/${resourceName}`);
        const form = h("form", { className: "form-card", onsubmit: async (e) => {
            e.preventDefault();
            const description = form.querySelector('[name="description"]').value.trim();
            const usersRaw = form.querySelector('[name="users"]').value.trim();
            const users = usersRaw ? usersRaw.split("\n").map(u => u.trim()).filter(Boolean) : [];
            try {
                await api(`/api/contracts/${contractNumber}/projects/${resourceName}`, {
                    method: "PATCH", body: JSON.stringify({ description, users }),
                });
                navigate(`/contracts/${cn}/projects/${rn}`);
            } catch (err) { showAlert(err.message); }
        }},
            h("label", {}, "Project name"),
            h("input", { value: p.name, disabled: "true" }),
            h("label", {}, "Description"),
            h("input", { name: "description", value: p.description, placeholder: "Optional description" }),
            h("label", {}, "Users (one identifier per line)"),
            h("textarea", { name: "users" }, p.users.join("\n")),
            h("div", { className: "btn-row" },
                h("a", { href: `#/contracts/${cn}/projects/${rn}`, className: "btn btn-secondary btn-small", style: "text-decoration:none" }, "Cancel"),
                h("button", { type: "submit", className: "btn btn-primary btn-small" }, "Save Changes"),
            ),
        );
        app.appendChild(form);
    } catch (e) { showAlert(e.message); }
}

// ========== ADMIN VIEWS ==========

function renderAdmin() {
    clear(app);
    app.appendChild(breadcrumbs({ label: "Admin" }));
    app.appendChild(h("h2", {}, "Admin"));
    app.appendChild(h("p", { className: "page-desc" },
        "SUNET-only management surfaces."));

    const tile = (href, title, desc) =>
        h("a", { href, className: "card card-clickable",
                  style: "display:block;text-decoration:none;color:inherit;margin-bottom:12px" },
            h("div", { className: "card-header" }, h("h3", {}, title)),
            h("p", { className: "meta" }, desc),
        );

    app.appendChild(tile("#/admin/customers", "Customers & Contracts",
        "Create customer organisations, contracts, and grant user access to contracts."));
    app.appendChild(tile("#/admin/clusters", "Tenant Clusters",
        "Register Kubernetes clusters, mark as provisioned, manage admin access. Includes the bootstrap setup guide."));
    app.appendChild(tile("#/admin/cluster-requests", "Cluster Change Requests",
        "Review and apply customer-admin requests for addons (JupyterHub), resizes, and backup enablement."));
    app.appendChild(tile("#/admin/billing", "Billing Jobs",
        "All scheduled billing exports across the platform — read-only view with manual-run support."));
    app.appendChild(tile("#/admin/pricing", "Pricing",
        "Per-resource unit prices, per-contract overrides, and rebates. Includes synthetic cluster fees."));
}

async function renderAdminCustomers() {
    clear(app);
    app.appendChild(breadcrumbs({ label: "Admin", hash: "admin" }, { label: "Customers" }));
    app.appendChild(h("h2", {}, "Customers"));
    app.appendChild(h("p", { className: "page-desc" }, "Manage customer organisations and their contracts."));

    const form = h("form", { onsubmit: async (e) => {
        e.preventDefault();
        const name = form.querySelector('[name="name"]').value.trim();
        const domain = form.querySelector('[name="domain"]').value.trim();
        const description = form.querySelector('[name="description"]').value.trim();
        try {
            await api("/api/admin/customers", { method: "POST", body: JSON.stringify({ name, domain, description }) });
            navigate("/admin");
        } catch (err) { showAlert(err.message); }
    }},
        h("div", { className: "form-card" },
            h("h3", {}, "Add Customer"),
            h("div", { className: "form-row" },
                h("div", {}, h("label", {}, "Name"), h("input", { name: "name", required: "true", placeholder: "Organisation name" })),
                h("div", {}, h("label", {}, "Domain"), h("input", { name: "domain", required: "true", placeholder: "example.se", pattern: "[a-z0-9.-]+" })),
            ),
            h("label", {}, "Description"),
            h("input", { name: "description", placeholder: "Optional" }),
            h("button", { type: "submit", className: "btn btn-primary btn-small" }, "Add Customer"),
        ),
    );
    app.appendChild(form);
    app.appendChild(h("div", { className: "section-label" }, "Existing Customers"));

    try {
        const customers = await api("/api/admin/customers");
        if (!customers.length) { app.appendChild(h("p", { className: "empty" }, "No customers yet.")); return; }
        for (const c of customers) {
            app.appendChild(
                h("a", { href: `#/admin/customers/${c.id}`, className: "card card-clickable", style: "display:block;text-decoration:none;color:inherit" },
                    h("div", { className: "card-header" }, h("h3", {}, c.name), h("span", { className: "badge badge-neutral" }, c.domain)),
                    c.description ? h("p", { className: "meta" }, c.description) : null,
                )
            );
        }
    } catch (e) { showAlert(e.message); }
}

async function renderAdminCustomerDetail(customerId) {
    clear(app);
    try {
        const customer = await api(`/api/admin/customers/${customerId}`);
        app.appendChild(breadcrumbs({ label: "Admin", hash: "admin" },{ label: "Customers", hash: "admin" }, { label: customer.name }));
        app.appendChild(h("h2", {}, customer.name));
        const descParts = [customer.domain];
        if (customer.description) descParts.push(customer.description);
        app.appendChild(h("p", { className: "page-desc" }, descParts.join(" — ")));

        app.appendChild(h("div", { className: "btn-row", style: "margin-bottom:20px" },
            h("a", { href: `#/admin/customers/edit/${customerId}`, className: "btn btn-secondary btn-small", style: "text-decoration:none" }, "Edit Customer"),
            h("button", { className: "btn btn-danger", onclick: async () => {
                if (confirm(`Delete customer ${customer.name}? All contracts must be deleted first.`)) {
                    try { await api(`/api/admin/customers/${customerId}`, { method: "DELETE" }); navigate("/admin"); }
                    catch (err) { showAlert(err.message); }
                }
            }}, "Delete Customer"),
        ));

        app.appendChild(h("div", { className: "section-label" }, "Add Contract"));
        const form = h("form", { onsubmit: async (e) => {
            e.preventDefault();
            const cn = form.querySelector('[name="contract_number"]').value.trim();
            const desc = form.querySelector('[name="description"]').value.trim();
            try {
                await api("/api/admin/contracts", { method: "POST", body: JSON.stringify({ customer_id: customerId, contract_number: cn, description: desc }) });
                navigate(`/admin/customers/${customerId}`);
            } catch (err) { showAlert(err.message); }
        }},
            h("div", { className: "form-card" },
                h("div", { className: "form-row" },
                    h("div", {}, h("label", {}, "Contract Number"), h("input", { name: "contract_number", required: "true", placeholder: "SD-123-a", pattern: "[A-Za-z0-9-]+" })),
                    h("div", {}, h("label", {}, "Description"), h("input", { name: "description", placeholder: "Optional" })),
                ),
                h("button", { type: "submit", className: "btn btn-primary btn-small" }, "Add Contract"),
            ),
        );
        app.appendChild(form);

        app.appendChild(h("div", { className: "section-label" }, "Contracts"));
        if (!customer.contracts.length) app.appendChild(h("p", { className: "empty" }, "No contracts yet."));
        for (const c of customer.contracts) {
            app.appendChild(
                h("a", { href: `#/admin/contracts/${c.id}`, className: "card card-clickable", style: "display:block;text-decoration:none;color:inherit" },
                    h("div", { className: "card-header" }, h("h3", {}, c.contract_number)),
                    c.description ? h("p", { className: "meta" }, c.description) : null,
                )
            );
        }
    } catch (e) { showAlert(e.message); }
}

async function renderAdminEditCustomer(customerId) {
    clear(app);
    try {
        const customer = await api(`/api/admin/customers/${customerId}`);
        app.appendChild(breadcrumbs({ label: "Admin", hash: "admin" },{ label: "Customers", hash: "admin" }, { label: customer.name, hash: `admin/customers/${customerId}` }, { label: "Edit" }));
        app.appendChild(h("h2", {}, "Edit Customer"));

        const form = h("form", { className: "form-card", onsubmit: async (e) => {
            e.preventDefault();
            const name = form.querySelector('[name="name"]').value.trim();
            const domain = form.querySelector('[name="domain"]').value.trim();
            const description = form.querySelector('[name="description"]').value.trim();
            try {
                await api(`/api/admin/customers/${customerId}`, { method: "PATCH", body: JSON.stringify({ name, domain, description }) });
                navigate(`/admin/customers/${customerId}`);
            } catch (err) { showAlert(err.message); }
        }},
            h("label", {}, "Name"),
            h("input", { name: "name", required: "true", value: customer.name }),
            h("label", {}, "Domain"),
            h("input", { name: "domain", required: "true", value: customer.domain, pattern: "[a-z0-9.-]+" }),
            h("label", {}, "Description"),
            h("input", { name: "description", value: customer.description }),
            h("div", { className: "btn-row" },
                h("a", { href: `#/admin/customers/${customerId}`, className: "btn btn-secondary btn-small", style: "text-decoration:none" }, "Cancel"),
                h("button", { type: "submit", className: "btn btn-primary btn-small" }, "Save Changes"),
            ),
        );
        app.appendChild(form);
    } catch (e) { showAlert(e.message); }
}

async function renderAdminContractDetail(contractId) {
    clear(app);
    try {
        const contract = await api(`/api/admin/contracts/${contractId}`);
        app.appendChild(breadcrumbs({ label: "Admin", hash: "admin" },{ label: "Customers", hash: "admin" }, { label: contract.customer.name, hash: `admin/customers/${contract.customer.id}` }, { label: contract.contract_number }));
        app.appendChild(h("h2", {}, contract.contract_number));
        const descParts = [contract.customer.name, contract.customer.domain];
        if (contract.description) descParts.push(contract.description);
        app.appendChild(h("p", { className: "page-desc" }, descParts.join(" — ")));

        app.appendChild(h("div", { className: "btn-row", style: "margin-bottom:20px" },
            h("a", { href: `#/admin/contracts/edit/${contractId}`, className: "btn btn-secondary btn-small", style: "text-decoration:none" }, "Edit Contract"),
            h("button", { className: "btn btn-danger", onclick: async () => {
                if (confirm(`Delete contract ${contract.contract_number}? All projects must be deleted first.`)) {
                    try { await api(`/api/admin/contracts/${contractId}`, { method: "DELETE" }); navigate(`/admin/customers/${contract.customer.id}`); }
                    catch (err) { showAlert(err.message); }
                }
            }}, "Delete Contract"),
        ));

        // Rebate
        app.appendChild(h("div", { className: "section-label" }, "Rebate"));
        const rebateForm = h("form", { className: "form-card", onsubmit: async (e) => {
            e.preventDefault();
            const pct = rebateForm.querySelector('[name="rebate"]').value.trim();
            try {
                await api(`/api/admin/contracts/${contractId}/rebate`, { method: "PUT", body: JSON.stringify({ rebate_percent: parseFloat(pct) }) });
                navigate(`/admin/contracts/${contractId}`);
            } catch (err) { showAlert(err.message); }
        }},
            h("div", { className: "form-row" },
                h("div", {},
                    h("label", {}, "Rebate (%)"),
                    h("input", { name: "rebate", type: "number", min: "0", max: "100", step: "0.01", value: contract.rebate_percent != null ? contract.rebate_percent : "" }),
                ),
                h("div", { style: "display:flex;align-items:flex-end;gap:8px;padding-bottom:12px" },
                    h("button", { type: "submit", className: "btn btn-primary btn-small" }, "Set Rebate"),
                    contract.rebate_percent != null ? h("button", { type: "button", className: "btn btn-danger", onclick: async () => {
                        try { await api(`/api/admin/contracts/${contractId}/rebate`, { method: "DELETE" }); navigate(`/admin/contracts/${contractId}`); }
                        catch (err) { showAlert(err.message); }
                    }}, "Remove") : null,
                ),
            ),
        );
        app.appendChild(rebateForm);

        // Price overrides
        app.appendChild(h("div", { className: "section-label" },
            "Price Overrides ",
            h("a", { href: "#/admin/pricing/docs", className: "help-link", style: "text-transform:none;letter-spacing:normal;font-weight:normal" }, "(how does pricing work?)"),
        ));
        try {
            const overrides = await api(`/api/admin/contracts/${contractId}/pricing`);
            if (overrides.length) {
                const ul = h("ul", { className: "user-list" });
                for (const o of overrides) {
                    ul.appendChild(h("li", {},
                        h("span", { className: "user-sub" }, `${o.resource_type}: ${o.unit_price} SEK`),
                        h("button", { className: "btn btn-danger", onclick: async () => {
                            await api(`/api/admin/contracts/${contractId}/pricing/${encodeURIComponent(o.resource_type)}`, { method: "DELETE" });
                            navigate(`/admin/contracts/${contractId}`);
                        }}, "Remove"),
                    ));
                }
                app.appendChild(ul);
            } else {
                app.appendChild(h("p", { className: "meta", style: "margin-bottom:8px" }, "Using global default prices."));
            }
        } catch (e) { /* ignore */ }

        // Fetch global prices for the dropdown
        let globalPrices = [];
        try { globalPrices = await api("/api/admin/pricing"); } catch (e) { /* ignore */ }

        if (globalPrices.length) {
            const select = h("select", { name: "resource_type", required: "true" },
                h("option", { value: "" }, "-- Select resource type --"),
                ...globalPrices.map(p => h("option", { value: p.resource_type }, `${p.resource_type} (${p.unit_price} SEK / ${p.unit})`)),
            );
            const priceForm = h("form", { className: "form-card", onsubmit: async (e) => {
                e.preventDefault();
                const rt = priceForm.querySelector('[name="resource_type"]').value;
                const price = priceForm.querySelector('[name="unit_price"]').value.trim();
                if (!rt) return;
                try {
                    await api(`/api/admin/contracts/${contractId}/pricing/${encodeURIComponent(rt)}`, {
                        method: "PUT", body: JSON.stringify({ resource_type: rt, unit_price: parseFloat(price) }),
                    });
                    navigate(`/admin/contracts/${contractId}`);
                } catch (err) { showAlert(err.message); }
            }},
                h("div", { className: "form-row" },
                    h("div", {}, h("label", {}, "Resource type"), select),
                    h("div", {}, h("label", {}, "Override price (SEK)"), h("input", { name: "unit_price", type: "number", min: "0", step: "0.01", required: "true" })),
                ),
                h("button", { type: "submit", className: "btn btn-primary btn-small" }, "Add Override"),
            );
            app.appendChild(priceForm);
        } else {
            app.appendChild(h("p", { className: "meta" }, "Configure global prices first (Admin > Pricing) before adding overrides."));
        }

        // Grant access
        app.appendChild(h("div", { className: "section-label" }, "Grant Access"));
        const accessForm = h("form", { onsubmit: async (e) => {
            e.preventDefault();
            const sub = accessForm.querySelector('[name="user_sub"]').value.trim();
            try {
                await api(`/api/admin/contracts/${contractId}/users`, { method: "POST", body: JSON.stringify({ user_sub: sub }) });
                navigate(`/admin/contracts/${contractId}`);
            } catch (err) { showAlert(err.message); }
        }},
            h("div", { className: "form-card" },
                h("div", { className: "form-row" },
                    h("div", {}, h("label", {}, "User identifier"), h("input", { name: "user_sub", required: "true", placeholder: "username@idp" })),
                    h("div", { style: "display:flex;align-items:flex-end;padding-bottom:12px" },
                        h("button", { type: "submit", className: "btn btn-primary btn-small" }, "Grant Access"),
                    ),
                ),
            ),
        );
        app.appendChild(accessForm);

        // Authorized users
        app.appendChild(h("div", { className: "section-label" }, "Authorized Users"));
        if (!contract.users.length) {
            app.appendChild(h("p", { className: "empty" }, "No users have access yet."));
        } else {
            const ul = h("ul", { className: "user-list" });
            for (const userSub of contract.users) {
                ul.appendChild(h("li", {},
                    h("span", { className: "user-sub" }, userSub),
                    h("button", { className: "btn btn-danger", onclick: async (e) => {
                        e.stopPropagation();
                        if (confirm(`Revoke access for ${userSub}?`)) {
                            await api(`/api/admin/contracts/${contractId}/users/${encodeURIComponent(userSub)}`, { method: "DELETE" });
                            navigate(`/admin/contracts/${contractId}`);
                        }
                    }}, "Revoke"),
                ));
            }
            app.appendChild(ul);
        }
    } catch (e) { showAlert(e.message); }
}

async function renderAdminEditContract(contractId) {
    clear(app);
    try {
        const contract = await api(`/api/admin/contracts/${contractId}`);
        app.appendChild(breadcrumbs({ label: "Admin", hash: "admin" },{ label: "Customers", hash: "admin" }, { label: contract.customer.name, hash: `admin/customers/${contract.customer.id}` }, { label: contract.contract_number, hash: `admin/contracts/${contractId}` }, { label: "Edit" }));
        app.appendChild(h("h2", {}, "Edit Contract"));

        const form = h("form", { className: "form-card", onsubmit: async (e) => {
            e.preventDefault();
            const description = form.querySelector('[name="description"]').value.trim();
            try {
                await api(`/api/admin/contracts/${contractId}`, { method: "PATCH", body: JSON.stringify({ description }) });
                navigate(`/admin/contracts/${contractId}`);
            } catch (err) { showAlert(err.message); }
        }},
            h("label", {}, "Contract number"),
            h("input", { value: contract.contract_number, disabled: "true" }),
            h("label", {}, "Description"),
            h("input", { name: "description", value: contract.description }),
            h("div", { className: "btn-row" },
                h("a", { href: `#/admin/contracts/${contractId}`, className: "btn btn-secondary btn-small", style: "text-decoration:none" }, "Cancel"),
                h("button", { type: "submit", className: "btn btn-primary btn-small" }, "Save Changes"),
            ),
        );
        app.appendChild(form);
    } catch (e) { showAlert(e.message); }
}

// --- Admin: Global Pricing ---

async function renderAdminPricing() {
    clear(app);
    app.appendChild(breadcrumbs({ label: "Admin", hash: "admin" },{ label: "Pricing" }));
    app.appendChild(h("h2", {}, "Global Pricing"));
    app.appendChild(h("p", { className: "page-desc" },
        "Set default prices per resource type. Prices can be set per metadata value (e.g. per flavor). Contracts can override these individually. ",
        h("a", { href: "#/admin/pricing/docs", className: "help-link" }, "How does pricing work?"),
    ));

    try {
        const prices = await api("/api/admin/pricing");
        if (prices.length) {
            const ul = h("ul", { className: "user-list" });
            for (const p of prices) {
                let label = p.resource_type;
                if (p.metadata_field && p.metadata_value)
                    label += ` [${p.metadata_field}=${p.metadata_value}]`;
                label += `: ${p.unit_price} SEK / ${p.unit}`;

                ul.appendChild(h("li", {},
                    h("span", { className: "user-sub" }, label),
                    h("button", { className: "btn btn-danger", onclick: async () => {
                        await api(`/api/admin/pricing/${p.id}`, { method: "DELETE" });
                        navigate("/admin/pricing");
                    }}, "Remove"),
                ));
            }
            app.appendChild(ul);
        } else {
            app.appendChild(h("p", { className: "empty" }, "No prices configured yet."));
        }
    } catch (e) { showAlert(e.message); }

    app.appendChild(h("div", { className: "section-label" }, "Add Price"));

    // Fetch available metrics from Gnocchi
    let metrics = [];
    try { metrics = await api("/api/admin/pricing/metrics"); } catch (e) { /* ignore */ }

    const metricUnits = {};
    const metricMeta = {};
    for (const m of metrics) {
        metricUnits[m.metric_type] = m.unit || "";
        metricMeta[m.metric_type] = m.metadata_fields || [];
    }

    // Metadata field/value dropdowns (shown when a metric with metadata is selected)
    const metaFieldContainer = h("div", { id: "meta-fields", style: "display:none" });

    const metricSelect = metrics.length
        ? h("select", { name: "resource_type", required: "true", onchange: (e) => {
            const rt = e.target.value;
            const fields = metricMeta[rt] || [];
            clear(metaFieldContainer);
            if (fields.length && fields[0].values.length) {
                const field = fields[0]; // primary metadata field (e.g. flavor_name)
                metaFieldContainer.style.display = "block";
                metaFieldContainer.appendChild(h("input", { type: "hidden", name: "metadata_field", value: field.field }));
                metaFieldContainer.appendChild(h("label", {}, `${field.field} (optional — leave blank for base price)`));
                metaFieldContainer.appendChild(
                    h("select", { name: "metadata_value" },
                        h("option", { value: "" }, "-- All (base price) --"),
                        ...field.values.map(v => h("option", { value: v }, v)),
                    )
                );
            } else {
                metaFieldContainer.style.display = "none";
            }
          }},
            h("option", { value: "" }, "-- Select metric --"),
            ...metrics.map(m => h("option", { value: m.metric_type }, `${m.metric_type} (${m.unit})`)),
          )
        : h("input", { name: "resource_type", required: "true", placeholder: "metric type (Gnocchi unavailable)" });

    const form = h("form", { className: "form-card", onsubmit: async (e) => {
        e.preventDefault();
        const rt = form.querySelector('[name="resource_type"]').value.trim();
        const price = form.querySelector('[name="unit_price"]').value.trim();
        const unit = metricUnits[rt] || "hours";
        const metaField = form.querySelector('[name="metadata_field"]');
        const metaValue = form.querySelector('[name="metadata_value"]');
        if (!rt) return;

        const body = { resource_type: rt, unit_price: parseFloat(price), unit };
        if (metaField && metaValue && metaValue.value) {
            body.metadata_field = metaField.value;
            body.metadata_value = metaValue.value;
        }
        try {
            await api("/api/admin/pricing", { method: "POST", body: JSON.stringify(body) });
            navigate("/admin/pricing");
        } catch (err) { showAlert(err.message); }
    }},
        h("div", { className: "form-row" },
            h("div", {}, h("label", {}, "Resource type"), metricSelect),
            h("div", {}, h("label", {}, "Unit price (SEK per hour)"), h("input", { name: "unit_price", type: "number", min: "0", step: "0.01", required: "true", placeholder: "0.00" })),
        ),
        metaFieldContainer,
        h("p", { className: "meta", style: "margin-bottom:12px" },
            "The billing system automatically detects the collection interval from Gnocchi and converts to hours."
        ),
        h("button", { type: "submit", className: "btn btn-primary btn-small" }, "Add Price"),
    );
    app.appendChild(form);

    if (!metrics.length) {
        app.appendChild(h("p", { className: "meta" }, "Could not connect to Gnocchi to discover available metrics. You can enter metric types manually."));
    }
}

// --- Pricing Documentation ---

function renderPricingDocs() {
    clear(app);
    app.appendChild(breadcrumbs({ label: "Admin", hash: "admin" },{ label: "Pricing", hash: "admin/pricing" }, { label: "Documentation" }));
    app.appendChild(h("h2", {}, "How Pricing Works"));

    const doc = h("div", { className: "doc" });
    doc.innerHTML = `
        <h3>Overview</h3>
        <p>The billing system queries <strong>Gnocchi</strong> (the metrics database) for resource usage data,
        then applies the prices you configure here to calculate costs for each contract.</p>

        <p>The pipeline is: <code>Ceilometer</code> (collects metrics) &rarr; <code>Gnocchi</code> (stores time-series data)
        &rarr; <code>Portal billing</code> (queries usage, applies prices, generates CSV).</p>

        <h3>How metering works</h3>
        <p>Ceilometer polls OpenStack services at a fixed interval and stores measurements in Gnocchi.
        Each measurement is a <strong>data point</strong> — one sample taken at one point in time.</p>

        <p>The billing system <strong>automatically detects</strong> the collection interval by examining
        the timestamps in Gnocchi's data. This means you don't need to worry about the interval — if
        it changes, billing adapts automatically. All usage is converted to <strong>hours</strong> before
        pricing is applied.</p>

        <h3>Resource types and metrics</h3>
        <p>When you add a price, you select a <strong>resource type</strong> from the dropdown. These are the metrics
        that Gnocchi is collecting. The dropdown also shows available metadata values (like flavor names) so you
        can set prices at the right granularity.</p>

        <table>
            <tr><th>Metric</th><th>What it measures</th><th>Priced per</th></tr>
            <tr><td>instance</td><td>Virtual machine existence (1 = running)</td><td>hour per instance</td></tr>
            <tr><td>volume.size</td><td>Block storage volume size</td><td>hour per GB</td></tr>
            <tr><td>image.size</td><td>Glance image size</td><td>hour per MB</td></tr>
            <tr><td>ip.floating</td><td>Floating IP allocation</td><td>hour per IP</td></tr>
            <tr><td>radosgw.objects.size</td><td>S3/object storage usage</td><td>hour per GB</td></tr>
            <tr><td>network.incoming.bytes.rate</td><td>Inbound network traffic rate</td><td>hour per MB</td></tr>
            <tr><td>network.outgoing.bytes.rate</td><td>Outbound network traffic rate</td><td>hour per MB</td></tr>
        </table>

        <h3>Metadata-based pricing</h3>
        <p>Some metrics have <strong>metadata fields</strong> that allow more granular pricing. For example,
        the <code>instance</code> metric includes <code>flavor_name</code>, so you can set different prices
        for different VM sizes. The <code>volume.size</code> metric includes <code>volume_type</code>
        for differentiating fast vs large storage.</p>

        <p>When billing, the system matches prices in this order:</p>
        <ol style="margin:0 0 12px 20px">
            <li><strong>Specific price</strong> — matches both the metric type AND the metadata value (e.g. instance where flavor_name = b2.c4r8)</li>
            <li><strong>Base price</strong> — matches just the metric type (e.g. instance with no metadata filter, used as fallback)</li>
        </ol>

        <p>This means you can set a base price for all instances, then set specific prices for individual flavors.</p>

        <h3>Setting prices</h3>
        <p>All prices are in <strong>SEK per hour</strong>. If your published price list uses monthly rates,
        divide by 730 (average hours per month) to get the hourly rate.</p>

        <div class="example">
            <p><strong>Example: VM flavor b2.c4r8 at 1,095 SEK/month</strong></p>
            <p>1. Hourly rate: 1,095 &divide; 730 = <strong>1.50 SEK/hour</strong></p>
            <p>2. Select resource type: <code>instance</code></p>
            <p>3. Select flavor_name: <code>b2.c4r8</code></p>
            <p>4. Set unit price: <code>1.50</code></p>
            <p>5. Result: an instance running all month = 730 &times; 1.50 = 1,095 SEK</p>
        </div>

        <div class="example">
            <p><strong>Example: Block storage (large) at 1.73 SEK/GB/month</strong></p>
            <p>1. Hourly rate: 1.73 &divide; 730 = <strong>0.00237 SEK/GB/hour</strong></p>
            <p>2. Select resource type: <code>volume.size</code></p>
            <p>3. Set unit price: <code>0.00237</code></p>
            <p>4. Result: 100 GB for a full month = 100 &times; 730 &times; 0.00237 = 173 SEK</p>
        </div>

        <div class="example">
            <p><strong>Example: S3 storage at 0.36 SEK/GB/month</strong></p>
            <p>1. Hourly rate: 0.36 &divide; 730 = <strong>0.000493 SEK/GB/hour</strong></p>
            <p>2. Select resource type: <code>radosgw.objects.size</code></p>
            <p>3. Set unit price: <code>0.000493</code></p>
        </div>

        <h3>Contract overrides and rebates</h3>
        <p><strong>Price overrides</strong> let you set a different hourly price for a specific contract,
        overriding the global default. This is useful for customers with negotiated rates.</p>

        <p><strong>Rebates</strong> are a percentage discount applied after the price calculation.
        A 10% rebate on a 1,000 SEK charge results in 900 SEK.</p>

        <p>The calculation: <code>hours &times; unit_price &times; (1 - rebate%/100) = cost</code></p>
    `;
    app.appendChild(doc);

    app.appendChild(h("div", { style: "margin-top:24px" },
        h("a", { href: "#/admin/pricing", className: "btn btn-secondary btn-small", style: "text-decoration:none" }, "Back to Pricing"),
    ));
}

// ========== BILLING VIEWS ==========

async function renderBillingJobs() {
    clear(app);
    app.appendChild(breadcrumbs({ label: "Billing" }));
    app.appendChild(h("h2", {}, "Billing Jobs"));
    app.appendChild(h("p", { className: "page-desc" }, "Automated billing exports delivered to WebDAV or email on a schedule."));
    app.appendChild(h("a", { href: "#/billing/new", className: "btn btn-primary btn-small", style: "display:inline-block;margin-bottom:16px;text-decoration:none" }, "+ New Billing Job"));

    try {
        const jobs = await api("/api/billing/jobs");
        if (!jobs.length) {
            app.appendChild(h("p", { className: "empty" }, "No billing jobs configured yet."));
            return;
        }
        for (const j of jobs) {
            app.appendChild(
                h("a", { href: `#/billing/${j.id}`, className: "card card-clickable", style: "display:block;text-decoration:none;color:inherit" },
                    h("div", { className: "card-header" },
                        h("h3", {}, j.name),
                        h("span", { className: j.enabled ? "badge badge-ready" : "badge badge-neutral" }, j.enabled ? "Enabled" : "Disabled"),
                    ),
                    h("p", { className: "meta" }, `${j.delivery_method} — ${j.schedule} — ${j.all_contracts ? "all contracts" : j.contract_ids.length + " contracts"}`),
                )
            );
        }
    } catch (e) { showAlert(e.message); }
}

async function renderBillingJobDetail(jobId) {
    clear(app);
    try {
        const job = await api(`/api/billing/jobs/${jobId}`);
        app.appendChild(breadcrumbs({ label: "Billing", hash: "billing" }, { label: job.name }));
        app.appendChild(h("h2", {}, job.name));

        app.appendChild(h("div", { className: "card" },
            h("div", { className: "card-header" },
                h("div", { className: "section-label", style: "margin:0" }, "Status"),
                h("span", { className: job.enabled ? "badge badge-ready" : "badge badge-neutral" }, job.enabled ? "Enabled" : "Disabled"),
            ),
            h("div", { className: "section-label" }, "Schedule"),
            h("p", {}, job.schedule),
            h("div", { className: "section-label" }, "Delivery"),
            h("p", {}, job.delivery_method === "webdav" ? `WebDAV: ${job.delivery_config.url || ""}` : `Email: ${job.delivery_config.recipient || ""}`),
            h("div", { className: "section-label" }, "Filename Template"),
            h("p", {}, job.filename_template),
            h("div", { className: "section-label" }, "Output Mode"),
            h("p", {}, job.per_contract ? "One file per contract" : "Single file"),
            h("div", { className: "section-label" }, "Contracts"),
            h("p", {}, job.all_contracts ? "All accessible contracts" : `${job.contract_ids.length} selected`),
        ));

        app.appendChild(h("div", { className: "btn-row", style: "margin-top:16px;margin-bottom:20px" },
            h("a", { href: `#/billing/${jobId}/edit`, className: "btn btn-secondary btn-small", style: "text-decoration:none" }, "Edit"),
            h("button", { className: "btn btn-primary btn-small", onclick: async () => {
                try {
                    const run = await api(`/api/billing/jobs/${jobId}/run`, { method: "POST", body: JSON.stringify({}) });
                    showAlert(`Run completed: ${run.status}${run.files_delivered ? ", " + run.files_delivered + " files delivered" : ""}`, run.status === "success" ? "success" : "error");
                    navigate(`/billing/${jobId}`);
                } catch (err) { showAlert(err.message); }
            }}, "Run Now"),
            h("button", { className: "btn btn-danger", onclick: async () => {
                if (confirm(`Delete billing job "${job.name}"?`)) {
                    await api(`/api/billing/jobs/${jobId}`, { method: "DELETE" });
                    navigate("/billing");
                }
            }}, "Delete"),
        ));

        // Execution history
        app.appendChild(h("div", { className: "section-label" }, "Execution History"));
        const runs = await api(`/api/billing/jobs/${jobId}/runs`);
        if (!runs.length) {
            app.appendChild(h("p", { className: "empty" }, "No executions yet."));
        } else {
            for (const r of runs) {
                const statusClass = r.status === "success" ? "badge-ready" : r.status === "error" ? "badge-error" : "badge-pending";
                app.appendChild(h("div", { className: "card" },
                    h("div", { className: "card-header" },
                        h("span", {}, new Date(r.started_at).toLocaleString()),
                        h("span", { className: `badge ${statusClass}` }, r.status),
                    ),
                    h("p", { className: "meta" },
                        `Period: ${r.billing_period_start.substring(0, 10)} to ${r.billing_period_end.substring(0, 10)}` +
                        (r.files_delivered ? ` — ${r.files_delivered} files delivered` : ""),
                    ),
                    r.error_message ? h("p", { className: "meta", style: "color:var(--error)" }, r.error_message) : null,
                ));
            }
        }
    } catch (e) { showAlert(e.message); }
}

async function renderCreateBillingJob() {
    clear(app);
    app.appendChild(breadcrumbs({ label: "Billing", hash: "billing" }, { label: "New Job" }));
    app.appendChild(h("h2", {}, "New Billing Job"));

    // Fetch user's contracts for selection
    const user = currentUser;
    const contracts = user.contracts || [];

    const form = h("form", { className: "form-card", onsubmit: async (e) => {
        e.preventDefault();
        const name = form.querySelector('[name="name"]').value.trim();
        const schedule = form.querySelector('[name="schedule"]').value.trim();
        const allContracts = form.querySelector('[name="all_contracts"]').checked;
        const deliveryMethod = form.querySelector('[name="delivery_method"]').value;
        const filenameTemplate = form.querySelector('[name="filename_template"]').value.trim();
        const perContract = form.querySelector('[name="per_contract"]').checked;

        const deliveryConfig = {};
        if (deliveryMethod === "webdav") {
            deliveryConfig.url = form.querySelector('[name="webdav_url"]').value.trim();
            deliveryConfig.username = form.querySelector('[name="webdav_username"]').value.trim();
            deliveryConfig.password = form.querySelector('[name="webdav_password"]').value;
        } else {
            deliveryConfig.recipient = form.querySelector('[name="email_recipient"]').value.trim();
        }

        const contractIds = [];
        if (!allContracts) {
            form.querySelectorAll('[name="contract_id"]:checked').forEach(cb => contractIds.push(parseInt(cb.value)));
        }

        try {
            await api("/api/billing/jobs", {
                method: "POST",
                body: JSON.stringify({ name, schedule, all_contracts: allContracts, contract_ids: contractIds, delivery_method: deliveryMethod, delivery_config: deliveryConfig, filename_template: filenameTemplate, per_contract: perContract }),
            });
            navigate("/billing");
        } catch (err) { showAlert(err.message); }
    }},
        h("label", {}, "Job name"),
        h("input", { name: "name", required: "true", placeholder: "Monthly billing export" }),

        h("label", {}, "Schedule (cron expression)"),
        h("input", { name: "schedule", required: "true", placeholder: "0 6 1 * *", value: "0 6 1 * *" }),
        h("p", { className: "meta", style: "margin-top:-8px;margin-bottom:12px" }, "e.g. 0 6 1 * * = 1st of each month at 06:00 UTC"),

        h("label", {}, "Contracts"),
        h("div", { style: "margin-bottom:12px" },
            h("label", { style: "display:inline;font-weight:normal;text-transform:none;letter-spacing:normal;color:var(--text)" },
                h("input", { type: "checkbox", name: "all_contracts", checked: "true", style: "width:auto;margin-right:6px" }),
                "All my contracts",
            ),
        ),
        h("div", { id: "contract-checkboxes", style: "margin-bottom:12px" },
            ...contracts.map(c =>
                h("label", { style: "display:block;font-weight:normal;text-transform:none;letter-spacing:normal;color:var(--text);padding:4px 0" },
                    h("input", { type: "checkbox", name: "contract_id", value: String(c.id), style: "width:auto;margin-right:6px" }),
                    c.contract_number + " (" + c.customer.name + ")",
                )
            ),
        ),

        h("label", {}, "Delivery method"),
        h("select", { name: "delivery_method", onchange: (e) => {
            const webdav = form.querySelector("#webdav-config");
            const email = form.querySelector("#email-config");
            webdav.style.display = e.target.value === "webdav" ? "block" : "none";
            email.style.display = e.target.value === "email" ? "block" : "none";
        }},
            h("option", { value: "webdav" }, "WebDAV"),
            h("option", { value: "email" }, "Email"),
        ),

        h("div", { id: "webdav-config" },
            h("label", {}, "WebDAV URL"),
            h("input", { name: "webdav_url", placeholder: "https://webdav.example.se/billing/" }),
            h("div", { className: "form-row" },
                h("div", {}, h("label", {}, "Username"), h("input", { name: "webdav_username" })),
                h("div", {}, h("label", {}, "Password"), h("input", { name: "webdav_password", type: "password" })),
            ),
        ),
        h("div", { id: "email-config", style: "display:none" },
            h("label", {}, "Recipient"),
            h("input", { name: "email_recipient", placeholder: "billing@example.se" }),
        ),

        h("label", {}, "Filename template"),
        h("input", { name: "filename_template", value: "billing-{year}-{month}.csv" }),
        h("p", { className: "meta", style: "margin-top:-8px;margin-bottom:12px" }, "Variables: {year}, {month}, {day}, {date}, {contract}"),

        h("div", { style: "margin-bottom:16px" },
            h("label", { style: "display:inline;font-weight:normal;text-transform:none;letter-spacing:normal;color:var(--text)" },
                h("input", { type: "checkbox", name: "per_contract", style: "width:auto;margin-right:6px" }),
                "Generate one file per contract",
            ),
        ),

        h("div", { className: "btn-row" },
            h("a", { href: "#/billing", className: "btn btn-secondary btn-small", style: "text-decoration:none" }, "Cancel"),
            h("button", { type: "submit", className: "btn btn-primary btn-small" }, "Create Job"),
        ),
    );
    app.appendChild(form);
}

async function renderEditBillingJob(jobId) {
    clear(app);
    try {
        const job = await api(`/api/billing/jobs/${jobId}`);
        app.appendChild(breadcrumbs({ label: "Billing", hash: "billing" }, { label: job.name, hash: `billing/${jobId}` }, { label: "Edit" }));
        app.appendChild(h("h2", {}, "Edit Billing Job"));

        const contracts = currentUser.contracts || [];

        const form = h("form", { className: "form-card", onsubmit: async (e) => {
            e.preventDefault();
            const name = form.querySelector('[name="name"]').value.trim();
            const schedule = form.querySelector('[name="schedule"]').value.trim();
            const allContracts = form.querySelector('[name="all_contracts"]').checked;
            const deliveryMethod = form.querySelector('[name="delivery_method"]').value;
            const filenameTemplate = form.querySelector('[name="filename_template"]').value.trim();
            const perContract = form.querySelector('[name="per_contract"]').checked;
            const enabled = form.querySelector('[name="enabled"]').checked;

            const deliveryConfig = {};
            if (deliveryMethod === "webdav") {
                deliveryConfig.url = form.querySelector('[name="webdav_url"]').value.trim();
                deliveryConfig.username = form.querySelector('[name="webdav_username"]').value.trim();
                deliveryConfig.password = form.querySelector('[name="webdav_password"]').value || "********";
            } else {
                deliveryConfig.recipient = form.querySelector('[name="email_recipient"]').value.trim();
            }

            const contractIds = [];
            if (!allContracts) {
                form.querySelectorAll('[name="contract_id"]:checked').forEach(cb => contractIds.push(parseInt(cb.value)));
            }

            try {
                await api(`/api/billing/jobs/${jobId}`, {
                    method: "PATCH",
                    body: JSON.stringify({ name, schedule, all_contracts: allContracts, contract_ids: contractIds, delivery_method: deliveryMethod, delivery_config: deliveryConfig, filename_template: filenameTemplate, per_contract: perContract, enabled }),
                });
                navigate(`/billing/${jobId}`);
            } catch (err) { showAlert(err.message); }
        }},
            h("label", {}, "Job name"),
            h("input", { name: "name", required: "true", value: job.name }),

            h("label", {}, "Schedule (cron expression)"),
            h("input", { name: "schedule", required: "true", value: job.schedule }),

            h("div", { style: "margin-bottom:12px" },
                h("label", { style: "display:inline;font-weight:normal;text-transform:none;letter-spacing:normal;color:var(--text)" },
                    h("input", { type: "checkbox", name: "all_contracts", style: "width:auto;margin-right:6px", ...(job.all_contracts ? { checked: "true" } : {}) }),
                    "All my contracts",
                ),
            ),
            h("div", { style: "margin-bottom:12px" },
                ...contracts.map(c =>
                    h("label", { style: "display:block;font-weight:normal;text-transform:none;letter-spacing:normal;color:var(--text);padding:4px 0" },
                        h("input", { type: "checkbox", name: "contract_id", value: String(c.id), style: "width:auto;margin-right:6px", ...(job.contract_ids.includes(c.id) ? { checked: "true" } : {}) }),
                        c.contract_number + " (" + c.customer.name + ")",
                    )
                ),
            ),

            h("label", {}, "Delivery method"),
            h("select", { name: "delivery_method", onchange: (e) => {
                form.querySelector("#webdav-config").style.display = e.target.value === "webdav" ? "block" : "none";
                form.querySelector("#email-config").style.display = e.target.value === "email" ? "block" : "none";
            }},
                h("option", { value: "webdav", ...(job.delivery_method === "webdav" ? { selected: "true" } : {}) }, "WebDAV"),
                h("option", { value: "email", ...(job.delivery_method === "email" ? { selected: "true" } : {}) }, "Email"),
            ),

            h("div", { id: "webdav-config", style: job.delivery_method === "webdav" ? "" : "display:none" },
                h("label", {}, "WebDAV URL"),
                h("input", { name: "webdav_url", value: (job.delivery_config.url || "") }),
                h("div", { className: "form-row" },
                    h("div", {}, h("label", {}, "Username"), h("input", { name: "webdav_username", value: (job.delivery_config.username || "") })),
                    h("div", {}, h("label", {}, "Password"), h("input", { name: "webdav_password", type: "password", placeholder: "Leave blank to keep current" })),
                ),
            ),
            h("div", { id: "email-config", style: job.delivery_method === "email" ? "" : "display:none" },
                h("label", {}, "Recipient"),
                h("input", { name: "email_recipient", value: (job.delivery_config.recipient || "") }),
            ),

            h("label", {}, "Filename template"),
            h("input", { name: "filename_template", value: job.filename_template }),

            h("div", { style: "margin-bottom:12px" },
                h("label", { style: "display:inline;font-weight:normal;text-transform:none;letter-spacing:normal;color:var(--text)" },
                    h("input", { type: "checkbox", name: "per_contract", style: "width:auto;margin-right:6px", ...(job.per_contract ? { checked: "true" } : {}) }),
                    "One file per contract",
                ),
            ),
            h("div", { style: "margin-bottom:16px" },
                h("label", { style: "display:inline;font-weight:normal;text-transform:none;letter-spacing:normal;color:var(--text)" },
                    h("input", { type: "checkbox", name: "enabled", style: "width:auto;margin-right:6px", ...(job.enabled ? { checked: "true" } : {}) }),
                    "Enabled",
                ),
            ),

            h("div", { className: "btn-row" },
                h("a", { href: `#/billing/${jobId}`, className: "btn btn-secondary btn-small", style: "text-decoration:none" }, "Cancel"),
                h("button", { type: "submit", className: "btn btn-primary btn-small" }, "Save Changes"),
            ),
        );
        app.appendChild(form);
    } catch (e) { showAlert(e.message); }
}

async function renderAdminBillingJobs() {
    clear(app);
    app.appendChild(breadcrumbs({ label: "Admin", hash: "admin" },{ label: "Billing Jobs" }));
    app.appendChild(h("h2", {}, "All Billing Jobs"));
    app.appendChild(h("p", { className: "page-desc" }, "All billing jobs across all users."));

    try {
        const jobs = await api("/api/billing/jobs?all=true");
        if (!jobs.length) {
            app.appendChild(h("p", { className: "empty" }, "No billing jobs configured."));
            return;
        }
        for (const j of jobs) {
            app.appendChild(
                h("a", { href: `#/billing/${j.id}`, className: "card card-clickable", style: "display:block;text-decoration:none;color:inherit" },
                    h("div", { className: "card-header" },
                        h("h3", {}, j.name),
                        h("span", { className: j.enabled ? "badge badge-ready" : "badge badge-neutral" }, j.enabled ? "Enabled" : "Disabled"),
                    ),
                    h("p", { className: "meta" }, `Owner: ${j.owner_sub} — ${j.delivery_method} — ${j.schedule}`),
                )
            );
        }
    } catch (e) { showAlert(e.message); }
}

// ========== TENANT CLUSTERS ==========

function statusBadge(status) {
    if (status === "active") return h("span", { className: "badge badge-ready" }, "active");
    if (status === "revoked") return h("span", { className: "badge badge-error" }, "revoked");
    if (status === "expired") return h("span", { className: "badge badge-pending" }, "expired");
    if (status === "pending") return h("span", { className: "badge badge-pending" }, "pending");
    if (status === "applied") return h("span", { className: "badge badge-ready" }, "applied");
    if (status === "denied") return h("span", { className: "badge badge-error" }, "denied");
    return h("span", { className: "badge badge-neutral" }, status || "?");
}

function fmtDate(iso) {
    if (!iso) return "—";
    try { return new Date(iso).toISOString().slice(0, 10); } catch { return iso; }
}

function daysUntil(iso) {
    if (!iso) return null;
    const ms = new Date(iso).getTime() - Date.now();
    return Math.floor(ms / (1000 * 60 * 60 * 24));
}

const ADDON_DISPLAY_NAMES = { jupyterhub: "JupyterHub" };

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
        if (before != null && tgt != null) {
            return `Resize from ${before} to ${tgt} worker groups (${3 + 3 * tgt} servers total)`;
        }
        if (tgt != null) {
            return `Resize to ${tgt} worker groups (${3 + 3 * tgt} servers total)`;
        }
        return "Resize";
    }
    if (r.request_type === "backup") {
        return p.action === "enable" ? "Enable backup" : "Disable backup";
    }
    return r.request_type;
}

async function renderClusters() {
    clear(app);
    app.appendChild(breadcrumbs(
        { label: "My Contracts", hash: "contracts" },
        { label: "All clusters" },
    ));
    app.appendChild(h("h2", {}, "All clusters"));
    app.appendChild(h("p", { className: "page-desc" }, "Tenant Kubernetes clusters you have access to across every contract. Open a contract page from My Contracts to see clusters scoped to that contract."));
    try {
        const clusters = await api("/api/clusters");
        if (!clusters.length) {
            app.appendChild(h("p", { className: "empty" }, "You don't have access to any clusters yet."));
            return;
        }
        for (const c of clusters) {
            app.appendChild(
                h("a", { href: `#/clusters/${encodeURIComponent(c.slug)}`, className: "card card-clickable", style: "display:block;text-decoration:none;color:inherit" },
                    h("div", { className: "card-header" },
                        h("h3", {}, c.name),
                        c.provisioned_at
                            ? h("span", { className: "badge badge-ready" }, "provisioned")
                            : h("span", { className: "badge badge-pending" }, "pending"),
                    ),
                    h("p", { className: "meta" }, `${c.size_label} — ${c.total_servers} servers (3 controllers + ${3 * c.worker_groups} workers)`),
                    h("p", { className: "meta" }, `Contract: ${c.contract_number} · Role: ${c.caller_role || "?"}`),
                    c.active_addons.length ? h("p", { className: "meta" }, "Addons: " + c.active_addons.join(", ")) : null,
                )
            );
        }
    } catch (e) { showAlert(e.message); }
}

async function renderClusterDetail(slug) {
    clear(app);
    try {
        const cluster = await api(`/api/clusters/${slug}`);
        const cn = encodeURIComponent(cluster.contract_number || "");
        app.appendChild(breadcrumbs(
            { label: "My Contracts", hash: "contracts" },
            { label: cluster.contract_number, hash: `contracts/${cn}/projects` },
            { label: slug },
        ));
        const isCustomerAdmin = cluster.caller_role === "customer_admin" || cluster.caller_role === "sunet_admin";

        const titleRow = [h("h2", {}, cluster.name)];
        titleRow.push(h("span", { className: "badge badge-neutral", style: "margin-left:8px;vertical-align:middle" }, cluster.size_label));
        if (cluster.provisioned_at) {
            titleRow.push(h("span", { className: "badge badge-ready", style: "margin-left:4px;vertical-align:middle" }, "provisioned"));
        } else {
            titleRow.push(h("span", { className: "badge badge-pending", style: "margin-left:4px;vertical-align:middle" }, "not provisioned"));
        }
        app.appendChild(h("div", { style: "display:flex;align-items:center;gap:6px;margin-bottom:8px" }, ...titleRow));

        // Overview card
        const overviewChildren = [
            h("div", { className: "section-label", style: "margin-top:0" }, "Cluster details"),
            h("p", { className: "meta" }, `Slug: ${cluster.slug}`),
            h("p", { className: "meta" }, `Servers: 3 controllers + ${3 * cluster.worker_groups} workers (${cluster.total_servers} total)`),
            h("p", { className: "meta" }, `Contract: ${cluster.contract_number}`),
            h("p", { className: "meta" }, `Your role: ${cluster.caller_role || "—"}`),
        ];
        if (cluster.management_project_resource_name) {
            overviewChildren.push(
                h("div", { className: "section-label" }, "Management project (read-only)"),
                h("p", {},
                    h("a", { href: `#/contracts/${encodeURIComponent(cluster.contract_number)}/projects/${encodeURIComponent(cluster.management_project_resource_name)}` },
                        cluster.management_project_resource_name)
                ),
            );
        }
        if (cluster.backup_project_resource_name) {
            overviewChildren.push(
                h("div", { className: "section-label" }, "Backup project"),
                h("p", {},
                    h("a", { href: `#/contracts/${encodeURIComponent(cluster.contract_number)}/projects/${encodeURIComponent(cluster.backup_project_resource_name)}` },
                        cluster.backup_project_resource_name)
                ),
            );
        }
        if (cluster.active_addons.length) {
            overviewChildren.push(
                h("div", { className: "section-label" }, "Active addons"),
                ...cluster.active_addons.map(a => h("p", {}, a)),
            );
        }
        app.appendChild(h("div", { className: "card", style: "margin-bottom:16px" }, ...overviewChildren));

        if (isCustomerAdmin) {
            app.appendChild(h("div", { className: "btn-row", style: "margin-bottom:16px" },
                h("a", { href: `#/clusters/${encodeURIComponent(slug)}/users`, className: "btn btn-secondary btn-small", style: "text-decoration:none" }, "Manage users"),
            ));
        }

        // Credentials section
        app.appendChild(h("h3", { style: "margin-top:24px;margin-bottom:8px" }, "My credentials"));
        if (!cluster.provisioned_at) {
            app.appendChild(h("p", { className: "meta" }, "Cluster is not yet provisioned — credential issuance is disabled."));
        } else {
            const issueForm = h("form", { className: "form-card", style: "margin-bottom:16px", onsubmit: async (e) => {
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
                    renderClusterDetail(slug);  // refresh list
                } catch (err) { showAlert(err.message); }
            }},
                h("label", {}, "Label (laptop, ci-runner, …)"),
                h("input", { name: "label", required: "true", maxlength: "128", placeholder: "laptop" }),
                h("label", {}, "TTL in days (optional, default 365)"),
                h("input", { name: "ttl_days", type: "number", min: "1", max: "3650" }),
                h("div", { className: "btn-row" },
                    h("button", { type: "submit", className: "btn btn-primary btn-small" }, "Issue kubeconfig"),
                ),
            );
            app.appendChild(issueForm);

            const creds = await api(`/api/clusters/${slug}/credentials`);
            if (!creds.length) {
                app.appendChild(h("p", { className: "empty" }, "You haven't issued any credentials yet."));
            } else {
                for (const c of creds) {
                    const expiryDays = daysUntil(c.expires_at);
                    const expiryWarn = c.status === "active" && expiryDays !== null && expiryDays < 30;
                    app.appendChild(h("div", { className: "card" },
                        h("div", { className: "card-header" },
                            h("h3", {}, c.label),
                            statusBadge(c.status),
                        ),
                        h("p", { className: "meta" }, `Issued: ${fmtDate(c.created_at)}`),
                        h("p", { className: "meta" }, `Expires: ${fmtDate(c.expires_at)}` + (expiryWarn ? ` (in ${expiryDays} days)` : "")),
                        h("p", { className: "meta" }, `Serial: ${c.cert_serial.substring(0, 16)}…`),
                        c.status === "active" ? h("div", { className: "btn-row" },
                            h("button", { className: "btn btn-secondary btn-small", onclick: async () => {
                                if (confirm(`Rotate credential "${c.label}"? The old kubeconfig will stop working.`)) {
                                    try {
                                        const issued = await api(`/api/clusters/${slug}/credentials/${c.id}/rotate`, { method: "POST" });
                                        showIssuedKubeconfig(issued);
                                        renderClusterDetail(slug);
                                    } catch (err) { showAlert(err.message); }
                                }
                            }}, "Rotate"),
                            h("button", { className: "btn btn-danger", onclick: async () => {
                                if (confirm(`Revoke credential "${c.label}"? This is immediate and cannot be undone.`)) {
                                    try {
                                        await api(`/api/clusters/${slug}/credentials/${c.id}`, { method: "DELETE" });
                                        renderClusterDetail(slug);
                                    } catch (err) { showAlert(err.message); }
                                }
                            }}, "Revoke"),
                        ) : null,
                    ));
                }
            }
        }

        // Load requests early so we can use them to disable conflicting buttons.
        const requests = await api(`/api/clusters/${slug}/requests`);

        // Cluster requests (request-a-change panel + history)
        if (isCustomerAdmin) {
            app.appendChild(h("h3", { style: "margin-top:24px;margin-bottom:8px" }, "Request a change"));
            app.appendChild(renderClusterRequestPanel(slug, cluster, requests));
        }

        app.appendChild(h("h3", { style: "margin-top:24px;margin-bottom:8px" }, "Request history"));
        if (!requests.length) {
            app.appendChild(h("p", { className: "empty" }, "No requests yet."));
        } else {
            for (const r of requests) {
                app.appendChild(h("div", { className: "card" },
                    h("div", { className: "card-header" },
                        h("h3", {}, formatRequestSummary(r)),
                        statusBadge(r.status),
                    ),
                    h("p", { className: "meta" }, `Requested by ${r.requested_by_sub} on ${fmtDate(r.requested_at)}`),
                    r.applied_at ? h("p", { className: "meta" }, `${r.status === "applied" ? "Applied" : "Denied"} by ${r.applied_by_sub || "?"} on ${fmtDate(r.applied_at)}`) : null,
                    r.note ? h("p", { className: "meta" }, `Note: ${r.note}`) : null,
                ));
            }
        }
    } catch (e) { showAlert(e.message); }
}

function renderClusterRequestPanel(slug, cluster, requests) {
    const panel = h("div", { className: "form-card" });
    const pendingByType = (type, predicate = () => true) =>
        (requests || []).some(r =>
            r.status === "pending" && r.request_type === type && predicate(r.payload || {})
        );

    // --- JupyterHub addon ---
    const jhActive = cluster.active_addons.includes("jupyterhub");
    const jhPending = pendingByType("addon", p => p.addon_type === "jupyterhub");
    const jhDisabled = jhActive || jhPending;
    const jhAttrs = {
        className: "btn btn-secondary btn-small",
        onclick: async () => {
            if (confirm("Request JupyterHub addon? SUNET ops will be notified by email.")) {
                try {
                    await api(`/api/clusters/${slug}/requests`, { method: "POST", body: JSON.stringify({
                        request_type: "addon",
                        payload: { action: "enable", addon_type: "jupyterhub" },
                    })});
                    renderClusterDetail(slug);
                } catch (err) { showAlert(err.message); }
            }
        },
    };
    if (jhDisabled) jhAttrs.disabled = "true";
    const jhLabel = jhActive
        ? "JupyterHub already enabled"
        : jhPending
            ? "JupyterHub request pending"
            : "Request JupyterHub addon";
    panel.appendChild(h("div", { style: "margin-bottom:12px" }, h("button", jhAttrs, jhLabel)));

    // --- Resize ---
    const resizePending = pendingByType("resize");
    const resizeBtnAttrs = {
        className: "btn btn-secondary btn-small",
        onclick: async () => {
            const input = panel.querySelector('[name="resize_target"]');
            const target = parseInt(input.value, 10);
            if (!target || target <= cluster.worker_groups) {
                showAlert("Target must be greater than current."); return;
            }
            if (confirm(`Request resize to ${target} worker groups (${3 + 3 * target} servers total)?`)) {
                try {
                    await api(`/api/clusters/${slug}/requests`, { method: "POST", body: JSON.stringify({
                        request_type: "resize",
                        payload: { target_worker_groups: target },
                    })});
                    renderClusterDetail(slug);
                } catch (err) { showAlert(err.message); }
            }
        },
    };
    if (resizePending) resizeBtnAttrs.disabled = "true";
    const resizeInputAttrs = {
        name: "resize_target", type: "number",
        min: cluster.worker_groups + 1,
        placeholder: `> ${cluster.worker_groups}`,
        style: "width:120px",
    };
    if (resizePending) resizeInputAttrs.disabled = "true";
    panel.appendChild(h("div", { style: "margin-bottom:12px" },
        h("label", {}, `Resize cluster (current: ${cluster.worker_groups} worker groups, ${3 * cluster.worker_groups} workers)`),
        h("div", { style: "display:flex;gap:8px" },
            h("input", resizeInputAttrs),
            h("button", resizeBtnAttrs, resizePending ? "Resize request pending" : "Request resize"),
        ),
    ));

    // --- Backup ---
    const backupEnabled = !!cluster.backup_project_resource_name;
    const backupPending = pendingByType("backup");
    const backupBtnAttrs = {
        className: "btn btn-secondary btn-small",
        onclick: async () => {
            const action = backupEnabled ? "disable" : "enable";
            if (confirm(`Request to ${action} backup?`)) {
                try {
                    await api(`/api/clusters/${slug}/requests`, { method: "POST", body: JSON.stringify({
                        request_type: "backup",
                        payload: { action },
                    })});
                    renderClusterDetail(slug);
                } catch (err) { showAlert(err.message); }
            }
        },
    };
    if (backupPending) backupBtnAttrs.disabled = "true";
    const backupLabel = backupPending
        ? "Backup request pending"
        : backupEnabled
            ? "Request to disable backup"
            : "Request to enable backup";
    panel.appendChild(h("div", {}, h("button", backupBtnAttrs, backupLabel)));

    return panel;
}

function showIssuedKubeconfig(issued) {
    const overlay = h("div", { className: "modal-overlay", style: "position:fixed;inset:0;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:100" });
    const close = () => overlay.remove();
    const blob = new Blob([issued.kubeconfig], { type: "text/yaml" });
    const url = URL.createObjectURL(blob);
    const dialog = h("div", { className: "card", style: "max-width:600px;width:90%;max-height:90vh;overflow:auto" },
        h("h2", {}, "Kubeconfig issued"),
        h("p", { className: "meta" }, `Label: ${issued.label} · Expires: ${fmtDate(issued.expires_at)}`),
        h("p", {}, "Save the file below — it is shown only once and not retrievable later."),
        h("textarea", { readonly: "true", style: "width:100%;height:200px;font-family:monospace;font-size:0.8rem;margin:8px 0" }, issued.kubeconfig),
        h("div", { className: "btn-row" },
            h("a", { href: url, download: `kubeconfig-${issued.cluster_slug}-${issued.label}.yaml`, className: "btn btn-primary btn-small", style: "text-decoration:none" }, "Download"),
            h("button", { className: "btn btn-secondary btn-small", onclick: close }, "Close"),
        ),
    );
    overlay.appendChild(dialog);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    document.body.appendChild(overlay);
}

async function renderClusterUsers(slug) {
    clear(app);
    app.appendChild(h("h2", {}, "Cluster users"));
    try {
        const cluster = await api(`/api/clusters/${slug}`);
        const users = await api(`/api/clusters/${slug}/users`);
        const cn = encodeURIComponent(cluster.contract_number || "");
        // Insert breadcrumbs above the title now that we know the contract.
        app.insertBefore(
            breadcrumbs(
                { label: "My Contracts", hash: "contracts" },
                { label: cluster.contract_number, hash: `contracts/${cn}/projects` },
                { label: slug, hash: `clusters/${encodeURIComponent(slug)}` },
                { label: "Users" },
            ),
            app.firstChild,
        );
        const isSunetAdmin = cluster.caller_role === "sunet_admin";

        for (const u of users) {
            app.appendChild(h("div", { className: "card" },
                h("div", { className: "card-header" },
                    h("h3", {}, u.user_sub),
                    h("span", { className: "badge badge-neutral" }, u.role),
                ),
                h("p", { className: "meta" }, `Granted by ${u.granted_by_sub} on ${fmtDate(u.created_at)}`),
                u.role !== "customer_admin" || isSunetAdmin ? h("div", { className: "btn-row" },
                    h("button", { className: "btn btn-danger", onclick: async () => {
                        if (confirm(`Remove ${u.user_sub}? All their kubeconfigs on this cluster will also be revoked.`)) {
                            try {
                                await api(`/api/clusters/${slug}/users/${encodeURIComponent(u.user_sub)}`, { method: "DELETE" });
                                renderClusterUsers(slug);
                            } catch (err) { showAlert(err.message); }
                        }
                    }}, "Remove"),
                ) : null,
            ));
        }

        // Add user form
        const form = h("form", { className: "form-card", style: "margin-top:16px", onsubmit: async (e) => {
            e.preventDefault();
            const user_sub = e.target.querySelector('[name="user_sub"]').value.trim();
            const role = e.target.querySelector('[name="role"]').value;
            try {
                await api(`/api/clusters/${slug}/users`, { method: "POST", body: JSON.stringify({ user_sub, role }) });
                renderClusterUsers(slug);
            } catch (err) { showAlert(err.message); }
        }},
            h("h3", {}, "Add user"),
            h("label", {}, "OIDC subject"),
            h("input", { name: "user_sub", required: "true", placeholder: "user@idp" }),
            h("label", {}, "Role"),
            h("select", { name: "role" },
                h("option", { value: "user" }, "user"),
                isSunetAdmin ? h("option", { value: "customer_admin" }, "customer_admin (SUNET admin only)") : null,
            ),
            h("div", { className: "btn-row" },
                h("button", { type: "submit", className: "btn btn-primary btn-small" }, "Grant access"),
            ),
        );
        app.appendChild(form);
    } catch (e) { showAlert(e.message); }
}

// ========== ADMIN: CLUSTERS ==========

async function renderAdminClusters() {
    clear(app);
    app.appendChild(breadcrumbs({ label: "Admin", hash: "admin" },{ label: "Clusters" }));
    app.appendChild(h("h2", {}, "All Tenant Clusters"));
    app.appendChild(h("div", { className: "btn-row", style: "margin-bottom:16px" },
        h("a", { href: "#/admin/clusters/new", className: "btn btn-primary btn-small", style: "text-decoration:none" }, "+ New Cluster"),
        h("a", { href: "#/admin/clusters/help", className: "btn btn-secondary btn-small", style: "text-decoration:none" }, "Setup guide"),
    ));
    try {
        const clusters = await api("/api/admin/clusters");
        if (!clusters.length) {
            app.appendChild(h("p", { className: "empty" }, "No clusters yet."));
            return;
        }
        for (const c of clusters) {
            app.appendChild(
                h("a", { href: `#/admin/clusters/${encodeURIComponent(c.slug)}`, className: "card card-clickable", style: "display:block;text-decoration:none;color:inherit" },
                    h("div", { className: "card-header" },
                        h("h3", {}, c.name),
                        c.provisioned_at
                            ? h("span", { className: "badge badge-ready" }, "provisioned")
                            : h("span", { className: "badge badge-pending" }, "pending"),
                    ),
                    h("p", { className: "meta" }, `${c.size_label} — ${c.total_servers} servers · contract ${c.contract_number}`),
                    h("p", { className: "meta" }, `${c.api_url}`),
                )
            );
        }
    } catch (e) { showAlert(e.message); }
}

async function renderAdminCreateCluster() {
    clear(app);
    app.appendChild(breadcrumbs(
        { label: "Admin" },
        { label: "Clusters", hash: "admin/clusters" },
        { label: "New" },
    ));
    app.appendChild(h("h2", {}, "Register Tenant Cluster"));

    let contracts = [];
    let customersById = {};
    try {
        contracts = await api("/api/admin/contracts");
        const customers = await api("/api/admin/customers");
        customersById = Object.fromEntries((customers || []).map(c => [c.id, c]));
    } catch (e) { showAlert(e.message); return; }

    if (!contracts.length) {
        app.appendChild(h("p", { className: "empty" }, "No contracts exist yet. Create a customer + contract first under Admin → Customers."));
        app.appendChild(h("a", { href: "#/admin", className: "btn btn-secondary btn-small", style: "display:inline-block;margin-top:12px;text-decoration:none" }, "Go to Customers"));
        return;
    }

    const contractOptions = [
        h("option", { value: "" }, "— select contract —"),
        ...contracts.map(c => {
            const customer = customersById[c.customer_id];
            const label = customer
                ? `${c.contract_number} — ${customer.name} (${customer.domain})`
                : c.contract_number;
            return h("option", { value: c.contract_number }, label);
        }),
    ];

    const form = h("form", { className: "form-card", onsubmit: async (e) => {
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
        h("label", {}, "Contract"),
        h("select", { name: "contract_number", required: "true" }, ...contractOptions),
        h("label", {}, "Display name"),
        h("input", { name: "name", required: "true", placeholder: "Acme production cluster" }),
        h("label", {}, "Slug (used in OpenBao mount path & cert O)"),
        h("input", { name: "slug", required: "true", pattern: "[a-z0-9]([a-z0-9-]*[a-z0-9])?", maxlength: "64", placeholder: "acme-prod" }),
        h("label", {}, "API URL"),
        h("input", { name: "api_url", required: "true", placeholder: "https://k8s.acme.example.org:6443" }),
        h("label", {}, "CA bundle (PEM)"),
        h("p", { className: "meta", style: "margin-top:-4px;margin-bottom:4px" },
            "Kubernetes CA from the tenant cluster. On a control-plane node: ",
            h("code", {}, "cat /etc/kubernetes/pki/ca.crt"),
            ". Or from an admin kubeconfig: ",
            h("code", {}, "kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d"),
            "."),
        h("textarea", { name: "ca_bundle", required: "true", placeholder: "-----BEGIN CERTIFICATE-----\n...", style: "min-height:120px;font-family:monospace" }),
        h("p", { className: "meta", style: "margin-top:-4px" }, "OpenBao mount will be derived as kubernetes/<slug>."),
        h("label", {}, "OpenBao role"),
        h("input", { name: "openbao_role", value: "argocd-rbac-manager" }),
        h("label", {}, "ArgoCD Role name (in argocd ns)"),
        h("input", { name: "argocd_role_name", value: "argocd-tenant" }),
        h("label", {}, "ArgoCD namespace"),
        h("input", { name: "argocd_namespace", value: "argocd" }),
        h("label", {}, "Worker groups (3 workers per group)"),
        h("input", { name: "worker_groups", type: "number", min: "1", value: "1", required: "true" }),
        h("div", { className: "btn-row" },
            h("a", { href: "#/admin/clusters", className: "btn btn-secondary btn-small", style: "text-decoration:none" }, "Cancel"),
            h("button", { type: "submit", className: "btn btn-primary btn-small" }, "Create Cluster"),
        ),
    );
    app.appendChild(form);
}

function renderClusterSetupHelp() {
    clear(app);
    app.appendChild(breadcrumbs(
        { label: "Admin" },
        { label: "Clusters", hash: "admin/clusters" },
        { label: "Setup guide" },
    ));
    app.appendChild(h("h2", {}, "Tenant Cluster Setup Guide"));
    app.appendChild(h("p", { className: "page-desc" },
        "Walks through every manual step needed to onboard a fresh tenant cluster. Run prerequisite steps once for the platform, then the per-cluster steps for each cluster you onboard."));

    const codeBlock = (txt) => h("pre", { className: "help-code" }, h("code", {}, txt));
    const section = (title) => h("h3", { style: "margin-top:24px;margin-bottom:8px" }, title);

    // ---- Platform prerequisites ----
    app.appendChild(section("Prerequisites (one-time, platform-wide)"));
    app.appendChild(h("p", {},
        "Done once on the platform side. Skip if these are already in place from a previous cluster onboarding."));

    app.appendChild(h("h4", { style: "margin-top:16px" }, "1. OpenBao Kubernetes auth method"));
    app.appendChild(h("p", {},
        "If `auth/kubernetes/` is not yet enabled in OpenBao, enable it and point it at the platform K8s API:"));
    app.appendChild(codeBlock(
        "bao auth enable kubernetes\n" +
        "bao write auth/kubernetes/config \\\n" +
        "    kubernetes_host=https://kubernetes.default.svc \\\n" +
        "    kubernetes_ca_cert=@/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    ));

    app.appendChild(h("h4", { style: "margin-top:16px" }, "2. Portal Vault policy + role"));
    app.appendChild(h("p", {},
        "Allows the portal pod to read short-lived ephemeral SA tokens from any per-tenant secrets engine."));
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
    app.appendChild(h("p", { className: "meta" },
        "The capability is ", h("code", {}, "update"),
        " (not ", h("code", {}, "read"),
        "): the kubernetes secrets engine generates credentials via POST,",
        " which Vault/OpenBao maps to ",
        h("code", {}, "update"),
        " — using ", h("code", {}, "read"),
        " yields a 403 permission-denied at credential mint time."));
    app.appendChild(h("p", { className: "meta" },
        "If the portal runs under a different SA name or namespace, adjust the bound_* fields. ttl=1h is how long the portal's Vault session lives between re-logins."));

    app.appendChild(h("h4", { style: "margin-top:16px" }, "3. Portal records"));
    app.appendChild(h("p", {},
        "The cluster will be tied to a contract under a customer. Create both first under ",
        h("a", { href: "#/admin" }, "Admin → Customers"),
        " if they don't exist yet."));

    // ---- Per-cluster ----
    app.appendChild(section("Per-cluster bootstrap"));
    app.appendChild(h("p", {},
        "Run these every time you onboard a new tenant cluster. We use ",
        h("code", {}, "<slug>"),
        " as the cluster identifier — it must match the slug you'll enter in the Register Tenant Cluster form."));

    app.appendChild(h("h4", { style: "margin-top:16px" }, "1. Provision the cluster"));
    app.appendChild(h("p", {},
        "Spin up the K8s cluster with kubespray and install ArgoCD into the ",
        h("code", {}, "argocd"),
        " namespace as usual. This guide picks up after that."));

    app.appendChild(h("h4", { style: "margin-top:16px" }, "2. Apply RBAC inside the tenant cluster"));
    app.appendChild(h("p", {},
        "Creates the Role customer kubeconfigs are bound to (",
        h("code", {}, "argocd-tenant"),
        " in the ",
        h("code", {}, "argocd"),
        " namespace), and the SA + ClusterRole that OpenBao authenticates as to mint per-issuance RoleBindings and CSRs."));
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

    app.appendChild(h("h4", { style: "margin-top:16px" }, "3. Configure OpenBao secrets engine for the cluster"));
    app.appendChild(h("p", {},
        "Mount a per-tenant ",
        h("code", {}, "kubernetes"),
        " secrets engine and tell it how to authenticate to the tenant cluster's API."));
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
    app.appendChild(h("p", { className: "meta" },
        "The path must be ",
        h("code", {}, "kubernetes/<slug>"),
        " — the portal derives the mount path from the cluster slug. ",
        "The role's ", h("code", {}, "allowed_kubernetes_namespaces"),
        " is a single entry (",
        h("code", {}, "kube-system"),
        ") because that's where the ",
        h("code", {}, "openbao-rbac-manager"),
        " SA lives. ",
        "TTL is set to 600s (10 min) because that's K8s's minimum for the ",
        "TokenRequest API; the portal uses the token only for the duration of a single ",
        "issuance call (a few seconds), so the actual exposure window is brief regardless."));

    app.appendChild(h("h4", { style: "margin-top:16px" }, "4. Register the cluster in the portal"));
    app.appendChild(h("p", {},
        "Open ",
        h("a", { href: "#/admin/clusters/new" }, "Admin → Clusters → + New Cluster"),
        ". Fill in the form — slug must match the ",
        h("code", {}, "<slug>"),
        " you used in step 3. The CA bundle field accepts the same PEM you passed to ",
        h("code", {}, "kubernetes_ca_cert"),
        " above; you can re-extract it with ",
        h("code", {}, "kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d"),
        "."));

    app.appendChild(h("h4", { style: "margin-top:16px" }, "5. Mark the cluster provisioned"));
    app.appendChild(h("p", {},
        "On the cluster's admin detail page, click ",
        h("strong", {}, "Mark provisioned"),
        ". This sets ",
        h("code", {}, "provisioned_at"),
        " and unlocks: (a) credential issuance for users; (b) the initial setup-fee billing line in the next billing run."));

    app.appendChild(h("h4", { style: "margin-top:16px" }, "6. Grant the first customer admin"));
    app.appendChild(h("p", {},
        "Open the cluster's user-facing detail page (",
        h("a", { href: "#/clusters" }, "My Clusters"),
        " — SUNET admins see all), click ",
        h("strong", {}, "Manage users"),
        ", and add at least one user with role ",
        h("code", {}, "customer_admin"),
        ". They can then add their own team members."));

    // ---- Verification ----
    app.appendChild(section("Verification"));
    app.appendChild(h("p", {},
        "Once the customer admin has issued their first kubeconfig:"));
    app.appendChild(h("ol", { style: "margin-left:24px" },
        h("li", {}, h("p", {}, "On the platform: ",
            h("code", {}, "kubectl --context <tenant-cluster> get csr"),
            " shows an Approved CSR signed by the cluster CA.")),
        h("li", {}, h("p", {}, "And: ",
            h("code", {}, "kubectl --context <tenant-cluster> get rolebindings -n argocd -l sunet.se/oidc-sub=<user-sub>"),
            " shows one binding per active credential for that user.")),
        h("li", {}, h("p", {}, "User-side: ",
            h("code", {}, "KUBECONFIG=./issued.yaml kubectl get applications -n argocd"),
            " succeeds; ",
            h("code", {}, "kubectl get pods -n kube-system"),
            " is forbidden (RBAC scoping confirmed).")),
    ));

    // ---- Decommissioning ----
    app.appendChild(section("Decommissioning"));
    app.appendChild(h("p", {},
        "To retire a tenant cluster, delete it from the admin clusters list (this also deletes its management/backup OpenStack project CRs and revokes all credentials). Then on the OpenBao side: ",
        h("code", {}, "bao secrets disable kubernetes/<slug>"),
        ". The OpenStack project will be torn down by the operator on next reconcile."));
}

async function renderAdminClusterDetail(slug) {
    clear(app);
    app.appendChild(breadcrumbs(
        { label: "Admin" },
        { label: "Clusters", hash: "admin/clusters" },
        { label: slug },
    ));
    try {
        const c = await api(`/api/admin/clusters/${slug}`);
        app.appendChild(h("h2", {}, c.name));
        app.appendChild(h("div", { className: "card", style: "margin-bottom:16px" },
            h("div", { className: "section-label", style: "margin-top:0" }, "Cluster"),
            h("p", { className: "meta" }, `Slug: ${c.slug}`),
            h("p", { className: "meta" }, `Size: ${c.size_label} (${c.total_servers} servers)`),
            h("p", { className: "meta" }, `API: ${c.api_url}`),
            h("p", { className: "meta" }, `Contract: ${c.contract_number}`),
            h("p", { className: "meta" }, `Provisioned: ${c.provisioned_at ? fmtDate(c.provisioned_at) : "(not yet)"}`),
            h("p", { className: "meta" }, `Management project: ${c.management_project_resource_name || "—"}`),
            h("p", { className: "meta" }, `Backup project: ${c.backup_project_resource_name || "—"}`),
        ));
        app.appendChild(h("div", { className: "btn-row" },
            !c.provisioned_at ? h("button", { className: "btn btn-primary btn-small", onclick: async () => {
                if (confirm("Mark this cluster as provisioned? This starts billing for the initial setup fee in the next billing run.")) {
                    try {
                        await api(`/api/admin/clusters/${slug}/provision`, { method: "POST" });
                        renderAdminClusterDetail(slug);
                    } catch (err) { showAlert(err.message); }
                }
            }}, "Mark provisioned") : null,
            h("a", { href: `#/clusters/${encodeURIComponent(slug)}`, className: "btn btn-secondary btn-small", style: "text-decoration:none" }, "Open as user"),
            h("button", { className: "btn btn-danger", onclick: async () => {
                if (confirm(`Delete cluster ${c.name}? This deletes the management/backup OpenStack projects and revokes all credentials. Cannot be undone.`)) {
                    try {
                        await api(`/api/admin/clusters/${slug}`, { method: "DELETE" });
                        navigate("/admin/clusters");
                    } catch (err) { showAlert(err.message); }
                }
            }}, "Delete cluster"),
        ));
    } catch (e) { showAlert(e.message); }
}

async function renderAdminClusterRequests() {
    clear(app);
    app.appendChild(breadcrumbs({ label: "Admin", hash: "admin" },{ label: "Cluster Requests" }));
    app.appendChild(h("h2", {}, "Pending cluster requests"));
    try {
        const requests = await api("/api/admin/cluster-requests?status=pending");
        if (!requests.length) {
            app.appendChild(h("p", { className: "empty" }, "No pending requests."));
        }
        for (const r of requests) {
            const card = h("div", { className: "card" },
                h("div", { className: "card-header" },
                    h("h3", {}, `${formatRequestSummary(r)} — ${r.cluster_slug}`),
                    statusBadge(r.status),
                ),
                h("p", { className: "meta" }, `Requested by ${r.requested_by_sub} on ${fmtDate(r.requested_at)}`),
                h("textarea", { name: `note-${r.id}`, placeholder: "Optional note", style: "width:100%;height:60px;margin-top:8px" }),
                h("div", { className: "btn-row" },
                    h("button", { className: "btn btn-primary btn-small", onclick: async () => {
                        const note = card.querySelector(`[name="note-${r.id}"]`).value.trim();
                        try {
                            await api(`/api/admin/cluster-requests/${r.id}/apply`, {
                                method: "POST", body: JSON.stringify({ note: note || null }),
                            });
                            renderAdminClusterRequests();
                        } catch (err) { showAlert(err.message); }
                    }}, "Apply"),
                    h("button", { className: "btn btn-danger", onclick: async () => {
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

        app.appendChild(h("h3", { style: "margin-top:24px;margin-bottom:8px" }, "Recent applied/denied"));
        const all = await api("/api/admin/cluster-requests");
        for (const r of all.filter(x => x.status !== "pending").slice(0, 20)) {
            app.appendChild(h("div", { className: "card" },
                h("div", { className: "card-header" },
                    h("h3", {}, `${formatRequestSummary(r)} — ${r.cluster_slug}`),
                    statusBadge(r.status),
                ),
                h("p", { className: "meta" }, `${r.status === "applied" ? "Applied" : "Denied"} by ${r.applied_by_sub} on ${fmtDate(r.applied_at)}`),
                r.note ? h("p", { className: "meta" }, `Note: ${r.note}`) : null,
            ));
        }
    } catch (e) { showAlert(e.message); }
}

// --- Init ---

route();
