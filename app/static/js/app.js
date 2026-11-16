/* ------------------------------------------------------------------
 * آرشیو هوشمند مطالب وب‌سایت‌ها — Frontend Controller
 * همه درخواست‌ها نسبت به همین دامنه و با مسیرهای /api/... ارسال می‌شوند.
 * ------------------------------------------------------------------ */

"use strict";

const state = {
    timezone: "Asia/Tehran",
    sites: [],
    sitesView: {
        page: 1,
        pageSize: "10",
        search: "",
        status: "all",
        source: "all",
    },
    posts: {
        page: 1,
        pageSize: 25,
        total: 0,
        totalPages: 1,
        items: [],
    },
    filters: {
        site_id: "",
        search: "",
        date_from: "",
        date_to: "",
        media_type: "all",
    },
    selectedPostIds: new Set(),
    editingSiteId: null,
    activeModalPost: null,
    overviewTimer: null,
    // روش‌های اتصال اختصاصی وب‌سایت‌ها (پروکسی per-site)
    connectionProfiles: [],
    connectionSites: [],
    connectionSummary: {},
    editingProfileId: null,
    connectionSiteFilter: "",
    connectionSelectedSites: new Set(),
};

const MEDIA_LABELS = {
    text: "متن",
    photo: "تصویر",
    video: "ویدیو",
    document: "فایل",
    voice: "صوت",
    audio: "صوت",
};

const TRIGGER_LABELS = {
    manual: "دستی",
    scheduled: "زمان‌بندی خودکار",
    demo: "نمونه آزمایشی",
};

const SOURCE_MODE_LABELS = {
    rss: "فید RSS/Atom",
    wp_api: "وب‌سرویس وردپرس",
    sitemap: "نقشه سایت (Sitemap)",
    html: "خزشگر HTML",
    auto: "هوشمند",
    demo: "نمونه",
};

const STATUS_LABELS = {
    ok: "موفق",
    success: "موفق",
    error: "خطا",
    pending: "در انتظار",
    syncing: "در حال دریافت",
};

/* ------------------------------ Helpers ------------------------------ */

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const FA_DIGITS = ["۰", "۱", "۲", "۳", "۴", "۵", "۶", "۷", "۸", "۹"];

function toFa(input) {
    if (input === null || input === undefined) return "—";
    return String(input).replace(/\d/g, (d) => FA_DIGITS[Number(d)]);
}

function escapeHtml(value) {
    if (value === null || value === undefined) return "";
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function formatNumberFa(value) {
    if (value === null || value === undefined || value === "") return "—";
    const num = Number(value);
    if (Number.isNaN(num)) return toFa(value);
    return toFa(num.toLocaleString("en-US"));
}

function formatBytesFa(bytes) {
    const n = Number(bytes || 0);
    if (!n) return "۰ کیلوبایت";
    if (n < 1024) return `${toFa(n)} بایت`;
    if (n < 1024 * 1024) return `${toFa((n / 1024).toFixed(1))} کیلوبایت`;
    return `${toFa((n / (1024 * 1024)).toFixed(2))} مگابایت`;
}

function intervalLabelFa(value, unit) {
    const unitMap = { minutes: "دقیقه", hours: "ساعت", days: "روز" };
    const unitFa = unitMap[unit] || unit;
    const v = Number(value);
    const pretty = Number.isInteger(v) ? v : Number(v.toFixed(2));
    return `هر ${toFa(pretty)} ${unitFa}`;
}

function splitDisplay(display) {
    if (!display) return { date: "—", time: "—" };
    const parts = String(display).split(" - ");
    if (parts.length === 2) return { date: parts[0], time: parts[1] };
    return { date: display, time: "" };
}

function showToast(message, type = "info", timeout = 4200) {
    const container = $("#toastContainer");
    if (!container) return;
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = message;
    container.appendChild(el);
    window.setTimeout(() => {
        el.style.opacity = "0";
        el.style.transition = "opacity 0.2s ease";
        window.setTimeout(() => el.remove(), 220);
    }, timeout);
}

async function apiFetch(path, options = {}) {
    const opts = Object.assign({ headers: {} }, options);
    if (opts.body && typeof opts.body !== "string") {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(opts.body);
    }
    opts.headers["Accept"] = "application/json";

    let response;
    try {
        response = await fetch(path, opts);
    } catch (_) {
        throw new Error("ارتباط با سرور برنامه برقرار نشد. لطفاً مطمئن شوید برنامه در حال اجراست.");
    }

    if (!response.ok) {
        let detail = `خطای سرور (کد ${response.status})`;
        try {
            const body = await response.json();
            if (body && body.detail) {
                detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
            }
        } catch (_) {
            /* ignore */
        }
        throw new Error(detail);
    }

    if (response.status === 204) return null;
    const contentType = response.headers.get("Content-Type") || "";
    if (contentType.includes("application/json")) return response.json();
    return response;
}

function setBusy(button, busy, busyLabel = "در حال انجام...") {
    if (!button) return;
    if (busy) {
        button.dataset.originalText = button.innerHTML;
        button.disabled = true;
        button.innerHTML = `<span class="spinner-inline"></span>${busyLabel}`;
    } else {
        button.disabled = false;
        if (button.dataset.originalText) button.innerHTML = button.dataset.originalText;
    }
}

function buildQuery(params) {
    const usp = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
        if (value === undefined || value === null) return;
        if (typeof value === "string" && value.trim() === "") return;
        if (value === "all") return;
        usp.set(key, String(value).trim());
    });
    const qs = usp.toString();
    return qs ? `?${qs}` : "";
}

/* ------------------------------ Tabs & Modals ------------------------------ */

function initTabs() {
    $$(".tab-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            $$(".tab-btn").forEach((b) => b.classList.remove("active"));
            $$(".tab-panel").forEach((p) => p.classList.remove("active"));
            btn.classList.add("active");
            const panel = document.getElementById(`tab-${btn.dataset.tab}`);
            if (panel) panel.classList.add("active");

            if (btn.dataset.tab === "logs") loadLogs();
            if (btn.dataset.tab === "settings") loadSettings();
            if (btn.dataset.tab === "cleanup") loadCleanupTab();
            if (btn.dataset.tab === "backup") {
                const box = $("#backupPreviewBox");
                if (box) box.classList.add("hidden");
            }
            if (btn.dataset.tab === "posts") loadPosts();
        });
    });
}

function initModals() {
    $$("[data-close-modal]").forEach((btn) => {
        btn.addEventListener("click", () => {
            const modal = document.getElementById(btn.dataset.closeModal);
            if (modal) modal.classList.add("hidden");
        });
    });

    $$(".modal-backdrop").forEach((backdrop) => {
        backdrop.addEventListener("click", (event) => {
            if (event.target === backdrop) backdrop.classList.add("hidden");
        });
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            $$(".modal-backdrop").forEach((m) => m.classList.add("hidden"));
        }
    });
}

/* ------------------------------ Overview ------------------------------ */

async function loadOverview() {
    try {
        const data = await apiFetch("/api/overview");
        const stats = data.stats || {};
        state.timezone = data.timezone || state.timezone;

        $("#statTotalSites").textContent = formatNumberFa(stats.total_sites);
        $("#statActiveSites").textContent = formatNumberFa(stats.active_sites);
        $("#statTotalPosts").textContent = formatNumberFa(stats.total_posts);
        $("#postsCountBadge").textContent = toFa(stats.total_posts || 0);

        const latest = splitDisplay(stats.latest_post_display);
        $("#statLatestPost").textContent = stats.latest_post_display
            ? `آخرین مطلب ذخیره‌شده: ${latest.date} ساعت ${latest.time}`
            : "هنوز مطلبی ذخیره نشده است";

        const nextFetch = splitDisplay(stats.nearest_next_fetch_display);
        $("#statNextFetch").textContent = stats.nearest_next_fetch_display
            ? `${nextFetch.date} ساعت ${nextFetch.time}`
            : "هیچ اجرای خودکاری در صف نیست";

        const modeLabels = {
            auto: "هوشمند (RSS + خزشگر وب)",
            rss: "فقط فیدخوان (RSS / Atom)",
            html: "فقط خزشگر صفحات وب (HTML)",
        };
        $("#statCollectorMode").textContent = modeLabels[data.collector_mode] || "هوشمند";
        $("#statDbInfo").textContent = `حجم SQLite: ${formatBytesFa(stats.db_size_bytes)}`;

        const scheduler = data.scheduler || {};
        const badge = $("#schedulerBadge");
        const badgeText = $("#schedulerBadgeText");
        if (scheduler.running) {
            badge.classList.remove("stopped");
            badge.classList.add("running");
            badgeText.textContent = `زمان‌بند خودکار فعال (هر ${toFa(scheduler.tick_seconds || 30)} ثانیه)`;
        } else {
            badge.classList.remove("running");
            badge.classList.add("stopped");
            badgeText.textContent = "زمان‌بند خودکار غیرفعال";
        }
    } catch (err) {
        console.error(err);
    }
}

/* ------------------------------ Websites ------------------------------ */

function statusBadge(site) {
    if (!site.is_active) {
        return '<span class="badge badge-paused">غیرفعال</span>';
    }
    if (site.last_status === "syncing") {
        return '<span class="badge badge-syncing">در حال دریافت</span>';
    }
    if (site.last_status === "error") {
        return '<span class="badge badge-error">خطای دریافت</span>';
    }
    if (site.last_status === "pending") {
        return '<span class="badge badge-pending">در انتظار اولین دریافت</span>';
    }
    return '<span class="badge badge-ok">فعال و پایش‌شده</span>';
}

/** برچسب کوچک «روش اتصال اختصاصی» برای جدول وب‌سایت‌ها */
function connectionBadgeHtml(site) {
    if (site && site.connection_profile_name) {
        return `<span class="badge badge-proxy" title="روش اتصال اختصاصی: ${escapeHtml(
            site.connection_label || ""
        )}">پروکسی اختصاصی</span>`;
    }
    if (site && site.proxy_url) {
        return `<span class="badge badge-proxy badge-proxy-manual" title="پروکسی دستی: ${escapeHtml(
            site.proxy_url
        )}">پروکسی دستی</span>`;
    }
    return "";
}

function normalizeSearchText(value) {
    if (value === null || value === undefined) return "";
    return String(value)
        .replace(/[۰-۹]/g, (d) => String("۰۱۲۳۴۵۶۷۸۹".indexOf(d)))
        .replace(/[٠-٩]/g, (d) => String("٠١٢٣٤٥٦٧٨٩".indexOf(d)))
        .trim()
        .toLowerCase();
}

function getFilteredSites() {
    const { search, status, source } = state.sitesView;
    const needle = normalizeSearchText(search);

    return state.sites.filter((site) => {
        if (status && status !== "all") {
            const isActive = Boolean(site.is_active);
            const lastStatus = String(site.last_status || "").toLowerCase();
            if (status === "inactive" && isActive) return false;
            if (status === "ok" && (!isActive || (lastStatus !== "ok" && lastStatus !== "success"))) {
                return false;
            }
            if (status === "error" && (!isActive || lastStatus !== "error")) return false;
            if (status === "pending" && (!isActive || lastStatus !== "pending")) return false;
            if (status === "syncing" && (!isActive || lastStatus !== "syncing")) return false;
        }

        if (source && source !== "all") {
            const hasFeed = Boolean(site.feed_url && String(site.feed_url).trim());
            if (source === "has_feed" && !hasFeed) return false;
            if (source === "no_feed" && hasFeed) return false;
        }

        if (needle) {
            const haystack = [
                site.title,
                site.canonical_url,
                site.domain,
                site.site_input,
                site.feed_url,
                site.last_error,
            ]
                .map(normalizeSearchText)
                .join(" ");
            if (!haystack.includes(needle)) return false;
        }

        return true;
    });
}

function buildCompactPageList(currentPage, totalPages) {
    if (totalPages <= 1) return [];
    if (totalPages <= 7) {
        return Array.from({ length: totalPages }, (_, idx) => idx + 1);
    }
    const pages = new Set([1, totalPages, currentPage, currentPage - 1, currentPage + 1]);
    const sorted = Array.from(pages)
        .filter((p) => p >= 1 && p <= totalPages)
        .sort((a, b) => a - b);
    const result = [];
    for (let i = 0; i < sorted.length; i += 1) {
        if (i > 0 && sorted[i] - sorted[i - 1] > 1) {
            result.push("...");
        }
        result.push(sorted[i]);
    }
    return result;
}

function updateSitesPaginationUI(totalFiltered, totalAll, currentPage, totalPages, startIdx, endIdx) {
    const info = $("#sitesPaginationInfo");
    const isFiltered =
        Boolean(normalizeSearchText(state.sitesView.search)) ||
        state.sitesView.status !== "all" ||
        state.sitesView.source !== "all";

    if (info) {
        if (totalAll === 0) {
            info.textContent = "نمایش ۰ از ۰ وب‌سایت";
        } else if (totalFiltered === 0) {
            info.textContent = `۰ نتیجه مطابق فیلتر (از مجموع ${toFa(totalAll)} وب‌سایت)`;
        } else if (isFiltered) {
            info.textContent = `نمایش ${toFa(startIdx + 1)} تا ${toFa(endIdx)} از ${toFa(
                totalFiltered
            )} نتیجه فیلتر (کل: ${toFa(totalAll)} وب‌سایت)`;
        } else {
            info.textContent = `نمایش ${toFa(startIdx + 1)} تا ${toFa(endIdx)} از مجموع ${toFa(
                totalAll
            )} وب‌سایت`;
        }
    }

    const indicator = $("#sitesPageIndicator");
    if (indicator) {
        indicator.textContent = `صفحه ${toFa(currentPage)} از ${toFa(totalPages)}`;
    }

    const prevBtn = $("#btnSitesPrevPage");
    const nextBtn = $("#btnSitesNextPage");
    if (prevBtn) prevBtn.disabled = currentPage <= 1;
    if (nextBtn) nextBtn.disabled = currentPage >= totalPages;

    const topSize = $("#sitesPageSize");
    const bottomSize = $("#sitesPageSizeBottom");
    if (topSize && topSize.value !== String(state.sitesView.pageSize)) {
        topSize.value = String(state.sitesView.pageSize);
    }
    if (bottomSize && bottomSize.value !== String(state.sitesView.pageSize)) {
        bottomSize.value = String(state.sitesView.pageSize);
    }

    const numsContainer = $("#sitesPageNumbers");
    if (numsContainer) {
        const items = buildCompactPageList(currentPage, totalPages);
        numsContainer.innerHTML = items
            .map((item) => {
                if (item === "...") {
                    return '<span class="muted">…</span>';
                }
                const activeCls = item === currentPage ? " active" : "";
                return `<button type="button" class="page-num-btn${activeCls}" data-site-page="${item}">${toFa(
                    item
                )}</button>`;
            })
            .join("");
    }
}

function renderSites() {
    const tbody = $("#sitesTableBody");
    if (!tbody) return;

    const totalAll = state.sites.length;
    if (!totalAll) {
        tbody.innerHTML = `
            <tr>
                <td colspan="8" class="empty-cell">
                    هنوز وب‌سایتی اضافه نکرده‌اید. از فرم بالا آدرس وب‌سایت یا صفحه اخبار را وارد کنید.
                    برای تست سریع بدون اینترنت نیز می‌توانید «بارگذاری داده نمونه آزمایشی» را بزنید.
                </td>
            </tr>`;
        updateSitesPaginationUI(0, 0, 1, 1, 0, 0);
        return;
    }

    const filtered = getFilteredSites();
    const totalFiltered = filtered.length;

    if (!totalFiltered) {
        tbody.innerHTML = `
            <tr>
                <td colspan="8" class="empty-cell">
                    هیچ وب‌سایتی مطابق با فیلتر انتخاب‌شده یافت نشد. می‌توانید دکمه «پاک‌سازی» را بزنید.
                </td>
            </tr>`;
        updateSitesPaginationUI(0, totalAll, 1, 1, 0, 0);
        return;
    }

    const rawSize = state.sitesView.pageSize;
    const isAll = rawSize === "all";
    const numericSize = isAll ? totalFiltered : Math.max(1, Number(rawSize) || 10);
    const totalPages = isAll ? 1 : Math.max(1, Math.ceil(totalFiltered / numericSize));
    if (state.sitesView.page > totalPages) {
        state.sitesView.page = totalPages;
    }
    if (state.sitesView.page < 1) {
        state.sitesView.page = 1;
    }

    const currentPage = state.sitesView.page;
    const startIdx = isAll ? 0 : (currentPage - 1) * numericSize;
    const endIdx = isAll ? totalFiltered : Math.min(startIdx + numericSize, totalFiltered);
    const visibleSites = filtered.slice(startIdx, endIdx);

    tbody.innerHTML = visibleSites
        .map((site) => {
            const cursorHtml = !site.initial_sync_done
                ? '<span class="badge badge-pending">— اولین دریافت انجام نشده —</span>'
                : `<span class="id-pill ltr-text" title="${escapeHtml(site.last_post_url || "")}">#${toFa(site.last_item_seq || 0)}</span>`;
            const lastFetched = splitDisplay(site.last_fetched_display);
            const nextFetch = splitDisplay(site.next_fetch_display);
            const demoBadge = site.is_demo ? '<span class="badge badge-demo">نمونه</span>' : "";
            const feedBadge = site.feed_url
                ? `<span class="badge badge-feed" title="فید شناسایی‌شده: ${escapeHtml(site.feed_url)}">RSS</span>`
                : "";
            const connectionBadge = connectionBadgeHtml(site);
            const errorHint = site.last_error
                ? `<div class="error-hint" title="${escapeHtml(site.last_error)}">${escapeHtml(
                      String(site.last_error).slice(0, 90)
                  )}${String(site.last_error).length > 90 ? "…" : ""}</div>`
                : "";

            return `
            <tr>
                <td>
                    <div class="ch-title-wrap">
                        <span class="ch-title">${escapeHtml(site.title)} ${demoBadge}${feedBadge}${connectionBadge}</span>
                        <a class="ch-link ltr-text" href="${escapeHtml(site.canonical_url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(site.canonical_url)}">
                            ${escapeHtml(site.canonical_url)}
                        </a>
                        <div class="ch-connection" title="روش اتصال این وب‌سایت برای دریافت مطالب">
                            روش اتصال: ${escapeHtml(site.connection_label || "حالت عمومی برنامه")}
                        </div>
                    </div>
                </td>
                <td>${intervalLabelFa(site.interval_value, site.interval_unit)}</td>
                <td>${cursorHtml}</td>
                <td><strong>${formatNumberFa(site.posts_count)}</strong></td>
                <td>${site.last_fetched_display
                    ? `<div class="date-stack"><span class="date-jalali">${toFa(lastFetched.date)}</span><span class="date-greg ltr-text">${toFa(lastFetched.time)}</span></div>`
                    : '<span class="muted">—</span>'}</td>
                <td>${site.is_active && site.next_fetch_display
                    ? `<div class="date-stack"><span class="date-jalali">${toFa(nextFetch.date)}</span><span class="date-greg ltr-text">${toFa(nextFetch.time)}</span></div>`
                    : '<span class="muted">—</span>'}</td>
                <td>${statusBadge(site)}${errorHint}</td>
                <td>
                    <div class="row-actions">
                        <button class="btn btn-outline btn-sm" data-action="sync" data-id="${site.id}">دریافت الان</button>
                        <button class="btn btn-ghost btn-sm" data-action="edit" data-id="${site.id}">ویرایش</button>
                        <button class="btn btn-ghost btn-sm" data-action="toggle" data-id="${site.id}">
                            ${site.is_active ? "غیرفعال" : "فعال"}
                        </button>
                        <button class="btn btn-danger-ghost btn-sm" data-action="delete" data-id="${site.id}">حذف</button>
                    </div>
                </td>
            </tr>`;
        })
        .join("");

    updateSitesPaginationUI(totalFiltered, totalAll, currentPage, totalPages, startIdx, endIdx);
}

async function loadSites() {
    try {
        const data = await apiFetch("/api/sites");
        state.sites = data.items || [];
        renderSites();
        populateSiteFilters();
        // همگام‌سازی فهرست وب‌سایت‌ها در بخش «روش اتصال اختصاصی» (زبانه تنظیمات)
        state.connectionSites = state.sites.map((s) => ({
            id: s.id,
            title: s.title,
            domain: s.domain,
            canonical_url: s.canonical_url,
            is_active: s.is_active,
            is_demo: s.is_demo,
            posts_count: s.posts_count,
            connection_profile_id: s.connection_profile_id,
            connection_profile_name: s.connection_profile_name,
            connection_label: s.connection_label,
            proxy_url: s.proxy_url,
        }));
        renderConnectionSiteList();
        updateConnectionSelectionUI();
    } catch (err) {
        showToast(err.message, "error");
    }
}

function fillSiteSelect(selector, { placeholder, showCount = false }) {
    const select = $(selector);
    if (!select) return;
    const previous = select.value;
    const head = `<option value="">${placeholder}</option>`;
    select.innerHTML =
        head +
        state.sites
            .map((s) => {
                const count = showCount ? ` (${toFa(s.posts_count || 0)} مطلب)` : "";
                const demo = s.is_demo ? " (نمونه)" : "";
                return `<option value="${s.id}">${escapeHtml(s.title)}${demo}${count}</option>`;
            })
            .join("");
    if (previous && state.sites.some((s) => String(s.id) === String(previous))) {
        select.value = previous;
    }
}

function populateSiteFilters() {
    fillSiteSelect("#filterSite", { placeholder: "همه وب‌سایت‌ها" });
    fillSiteSelect("#deleteScopeSite", { placeholder: "همه وب‌سایت‌ها" });
    fillSiteSelect("#deleteSingleSite", { placeholder: "انتخاب وب‌سایت...", showCount: true });
}

function initSiteForm() {
    const form = $("#addSiteForm");
    const intervalValue = $("#intervalValue");
    const intervalUnit = $("#intervalUnit");

    $$(".chip").forEach((chip) => {
        chip.addEventListener("click", () => {
            $$(".chip").forEach((c) => c.classList.remove("active"));
            chip.classList.add("active");
            intervalValue.value = chip.dataset.val;
            intervalUnit.value = chip.dataset.unit;
        });
    });

    [intervalValue, intervalUnit].forEach((el) => {
        el.addEventListener("input", () => {
            const match = $$(".chip").find(
                (c) =>
                    String(c.dataset.val) === String(intervalValue.value) &&
                    c.dataset.unit === intervalUnit.value
            );
            $$(".chip").forEach((c) => c.classList.toggle("active", c === match));
        });
    });

    if (!form) return;

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = $("#btnAddSite");

        const payload = {
            site_input: $("#siteInput").value.trim(),
            title: $("#siteTitle").value.trim() || null,
            interval_value: Number(intervalValue.value) || 6,
            interval_unit: intervalUnit.value,
            initial_limit: Number($("#initialLimit").value) || 10,
            is_active: true,
            fetch_now: $("#fetchNowCheck").checked,
        };

        if (!payload.site_input) {
            showToast("آدرس وب‌سایت را وارد کنید.", "error");
            return;
        }
        if (payload.interval_value <= 0) {
            showToast("بازه زمانی بررسی باید بزرگ‌تر از صفر باشد.", "error");
            return;
        }

        setBusy(button, true, "در حال افزودن و دریافت...");
        try {
            const res = await apiFetch("/api/sites", { method: "POST", body: payload });
            const site = res.site || {};
            let msg = `وب‌سایت «${site.title || payload.site_input}» با موفقیت اضافه شد.`;
            if (res.sync_result) {
                if (res.sync_result.status === "success") {
                    msg += ` ${toFa(res.sync_result.new_saved_count || 0)} مطلب جدید ذخیره شد.`;
                } else {
                    msg += ` اما دریافت اولیه انجام نشد: ${res.sync_result.message || ""}`;
                }
            }
            showToast(msg, res.sync_result && res.sync_result.status !== "success" ? "error" : "success");

            form.reset();
            $("#initialLimit").value = "10";
            $("#fetchNowCheck").checked = true;
            $("#intervalValue").value = "6";
            $("#intervalUnit").value = "hours";
            $$(".chip").forEach((c) =>
                c.classList.toggle("active", c.dataset.val === "6" && c.dataset.unit === "hours")
            );
            state.sitesView.page = 1;

            await Promise.all([loadSites(), loadOverview()]);
        } catch (err) {
            showToast(err.message, "error");
        } finally {
            setBusy(button, false);
        }
    });
}

function openEditModal(siteId) {
    const site = state.sites.find((s) => String(s.id) === String(siteId));
    if (!site) return;
    state.editingSiteId = site.id;
    document.getElementById("editSiteId").value = site.id;
    document.getElementById("editSiteTitle").value = site.title || "";
    document.getElementById("editIntervalValue").value = site.interval_value;
    document.getElementById("editIntervalUnit").value = site.interval_unit;
    document.getElementById("editInitialLimit").value = site.initial_limit;
    document.getElementById("editSiteActive").checked = Boolean(site.is_active);

    // روش اتصال اختصاصی (پروفایل) این وب‌سایت
    const profileSelect = document.getElementById("editSiteConnectionProfile");
    if (profileSelect) {
        fillConnectionProfileSelect(profileSelect, {
            placeholder: "حالت عمومی برنامه (پروکسی سراسری یا اتصال مستقیم)",
        });
        profileSelect.value = site.connection_profile_id ? String(site.connection_profile_id) : "";
    }

    // پروکسی دستی قدیمی (فقط زمانی که پروفایل اختصاصی نسبت داده نشده باشد)
    const manualProxy = !site.connection_profile_id && site.proxy_url ? String(site.proxy_url) : "";
    const parts = manualProxy.split("://");
    const type = parts.length === 2 ? parts[0] : "socks5";
    const hostPort = parts.length === 2 ? parts[1] : "";
    const [host, port] = hostPort.split(":");
    document.getElementById("editSiteProxyHost").value = host || "";
    document.getElementById("editSiteProxyPort").value = port || "";
    document.getElementById("editSiteProxyType").value = ["socks5", "http", "https"].includes(type)
        ? type
        : "socks5";

    document.getElementById("editSiteModal").classList.remove("hidden");
}

function initEditForm() {
    const form = document.getElementById("editSiteForm");
    if (!form) return;
    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const id = document.getElementById("editSiteId").value;
        if (!id) return;

        const profileValue = (document.getElementById("editSiteConnectionProfile")?.value || "").trim();

        const proxyHost = document.getElementById("editSiteProxyHost")?.value.trim() || "";
        const proxyPort = document.getElementById("editSiteProxyPort")?.value.trim() || "";
        const proxyType = document.getElementById("editSiteProxyType")?.value || "socks5";
        // رشته خالی = پاک‌کردن پروکسی دستی (اگر روش اتصال اختصاصی انتخاب شده باشد هم پاک می‌شود)
        const proxyUrl = !profileValue && proxyHost && proxyPort ? `${proxyType}://${proxyHost}:${proxyPort}` : "";

        const payload = {
            title: document.getElementById("editSiteTitle").value.trim(),
            interval_value: Number(document.getElementById("editIntervalValue").value),
            interval_unit: document.getElementById("editIntervalUnit").value,
            initial_limit: Number(document.getElementById("editInitialLimit").value),
            is_active: document.getElementById("editSiteActive").checked,
            proxy_url: proxyUrl,
            // null صریح = بازگشت وب‌سایت به حالت عمومی برنامه
            connection_profile_id: profileValue ? Number(profileValue) : null,
        };

        try {
            await apiFetch(`/api/sites/${id}`, { method: "PUT", body: payload });
            document.getElementById("editSiteModal").classList.add("hidden");
            showToast("تنظیمات وب‌سایت با موفقیت ذخیره شد.", "success");
            await Promise.all([loadSites(), loadOverview(), loadConnectionProfiles()]);
        } catch (err) {
            showToast(err.message, "error");
        }
    });
}


async function handleSiteAction(action, siteId, button) {
    const site = state.sites.find((s) => String(s.id) === String(siteId));

    if (action === "edit") {
        openEditModal(siteId);
        return;
    }

    if (action === "sync") {
        setBusy(button, true, "دریافت...");
        try {
            const res = await apiFetch(`/api/sites/${siteId}/sync`, { method: "POST" });
            const r = res.sync_result || {};
            if (r.status === "success") {
                showToast(
                    `${r.site_title || "وب‌سایت"}: ${toFa(r.new_saved_count || 0)} مطلب جدید ذخیره شد.`,
                    "success"
                );
            } else {
                showToast(r.message || "دریافت مطالب با خطا مواجه شد.", "error");
            }
            await Promise.all([loadSites(), loadOverview()]);
        } catch (err) {
            showToast(err.message, "error");
        } finally {
            setBusy(button, false);
        }
        return;
    }

    if (action === "toggle") {
        if (!site) return;
        try {
            await apiFetch(`/api/sites/${siteId}`, {
                method: "PUT",
                body: { is_active: !site.is_active },
            });
            showToast(
                `وب‌سایت «${site.title}» ${site.is_active ? "غیرفعال" : "فعال"} شد.`,
                "success"
            );
            await Promise.all([loadSites(), loadOverview()]);
        } catch (err) {
            showToast(err.message, "error");
        }
        return;
    }

    if (action === "delete") {
        if (!site) return;
        const confirmed = window.confirm(
            `آیا از حذف وب‌سایت «${site.title}» به همراه تمام مطالب ذخیره‌شده آن مطمئن هستید؟`
        );
        if (!confirmed) return;
        try {
            await apiFetch(`/api/sites/${siteId}`, { method: "DELETE" });
            showToast("وب‌سایت و مطالب مرتبط با آن حذف شد.", "success");
            await Promise.all([loadSites(), loadOverview(), loadPosts()]);
        } catch (err) {
            showToast(err.message, "error");
        }
    }
}

function initSiteTableEvents() {
    const tbody = $("#sitesTableBody");
    if (tbody) {
        tbody.addEventListener("click", (event) => {
            const button = event.target.closest("button[data-action]");
            if (!button) return;
            handleSiteAction(button.dataset.action, button.dataset.id, button);
        });
    }

    const filterForm = $("#siteFiltersForm");
    const searchInput = $("#siteFilterSearch");
    const statusSelect = $("#siteFilterStatus");
    const sourceSelect = $("#siteFilterSource");
    const topSizeSelect = $("#sitesPageSize");
    const bottomSizeSelect = $("#sitesPageSizeBottom");
    const clearBtn = $("#btnClearSiteFilters");
    const prevBtn = $("#btnSitesPrevPage");
    const nextBtn = $("#btnSitesNextPage");
    const pageNums = $("#sitesPageNumbers");

    const applyFilters = () => {
        state.sitesView.search = searchInput ? searchInput.value.trim() : "";
        state.sitesView.status = statusSelect ? statusSelect.value : "all";
        state.sitesView.source = sourceSelect ? sourceSelect.value : "all";
        state.sitesView.page = 1;
        renderSites();
    };

    if (filterForm) {
        filterForm.addEventListener("submit", (event) => {
            event.preventDefault();
            applyFilters();
        });
    }
    if (searchInput) {
        searchInput.addEventListener("input", applyFilters);
    }
    if (statusSelect) {
        statusSelect.addEventListener("change", applyFilters);
    }
    if (sourceSelect) {
        sourceSelect.addEventListener("change", applyFilters);
    }

    const handlePageSizeChange = (newSize) => {
        state.sitesView.pageSize = newSize || "10";
        state.sitesView.page = 1;
        renderSites();
    };
    if (topSizeSelect) {
        topSizeSelect.addEventListener("change", () => handlePageSizeChange(topSizeSelect.value));
    }
    if (bottomSizeSelect) {
        bottomSizeSelect.addEventListener("change", () => handlePageSizeChange(bottomSizeSelect.value));
    }

    if (clearBtn) {
        clearBtn.addEventListener("click", () => {
            if (searchInput) searchInput.value = "";
            if (statusSelect) statusSelect.value = "all";
            if (sourceSelect) sourceSelect.value = "all";
            state.sitesView.search = "";
            state.sitesView.status = "all";
            state.sitesView.source = "all";
            state.sitesView.page = 1;
            renderSites();
        });
    }

    if (prevBtn) {
        prevBtn.addEventListener("click", () => {
            if (state.sitesView.page > 1) {
                state.sitesView.page -= 1;
                renderSites();
            }
        });
    }
    if (nextBtn) {
        nextBtn.addEventListener("click", () => {
            state.sitesView.page += 1;
            renderSites();
        });
    }
    if (pageNums) {
        pageNums.addEventListener("click", (event) => {
            const btn = event.target.closest("button[data-site-page]");
            if (!btn) return;
            const targetPage = Number(btn.dataset.sitePage);
            if (targetPage >= 1) {
                state.sitesView.page = targetPage;
                renderSites();
            }
        });
    }
}

async function syncAllSites() {
    const button = $("#btnSyncAll");
    setBusy(button, true, "در حال دریافت از همه سایت‌ها...");
    try {
        const res = await apiFetch("/api/sites/sync-all", { method: "POST" });
        if (!res.checked_sites) {
            showToast("هیچ وب‌سایت فعالی برای دریافت وجود ندارد.", "info");
        } else {
            showToast(
                `بررسی ${toFa(res.checked_sites)} وب‌سایت انجام شد: ${toFa(res.success_count)} موفق، ${toFa(
                    res.error_count
                )} خطا و ${toFa(res.total_new_posts)} مطلب جدید.`,
                res.error_count ? "error" : "success",
                6000
            );
        }
        await Promise.all([loadSites(), loadOverview(), loadPosts(), loadLogs()]);
    } catch (err) {
        showToast(err.message, "error");
    } finally {
        setBusy(button, false);
    }
}

/* ------------------------------ Posts ------------------------------ */

function mediaBadge(mediaType) {
    const key = (mediaType || "text").toLowerCase();
    const label = MEDIA_LABELS[key] || key;
    const cls = key === "text" ? "badge-paused" : "badge-syncing";
    return `<span class="badge ${cls}">${escapeHtml(label)}</span>`;
}

function extractBodyWithoutTitle(post) {
    const title = (post.title || "").trim();
    const content = (post.content || "").trim();
    if (title && content.startsWith(title)) {
        const rest = content.slice(title.length).trim();
        return rest || content;
    }
    return content;
}

function renderPosts() {
    const tbody = $("#postsTableBody");
    if (!tbody) return;
    const items = state.posts.items;

    if (!items.length) {
        tbody.innerHTML = `
            <tr>
                <td colspan="9" class="empty-cell">
                    مطلبی مطابق با فیلترهای فعلی یافت نشد. اگر هنوز هیچ مطلبی ندارید، ابتدا یک وب‌سایت اضافه کنید یا داده نمونه را بارگذاری نمایید.
                </td>
            </tr>`;
    } else {
        tbody.innerHTML = items
            .map((p) => {
                const views = p.views === null || p.views === undefined ? "—" : `${formatNumberFa(p.views)} بازدید`;
                const headline = escapeHtml(p.title || "بدون عنوان");
                const snippet = escapeHtml(extractBodyWithoutTitle(p) || "");
                const checked = state.selectedPostIds.has(Number(p.id));
                return `
                <tr class="${checked ? "row-selected" : ""}">
                    <td class="select-col">
                        <input type="checkbox" class="row-select" data-post-select="${p.id}" ${checked ? "checked" : ""} />
                    </td>
                    <td>
                        <div class="ch-title-wrap">
                            <span class="ch-title">${escapeHtml(p.site_title)}</span>
                            <a class="ch-link ltr-text" href="${escapeHtml(
                                p.site_url || p.post_url
                            )}" target="_blank" rel="noopener noreferrer">${escapeHtml(
                    p.site_domain || ""
                )}</a>
                        </div>
                    </td>
                    <td><span class="id-pill ltr-text">#${toFa(p.item_seq)}</span></td>
                    <td>
                        <div class="date-stack">
                            <span class="date-jalali">${toFa(p.published_jalali_date)}</span>
                            <span class="date-greg ltr-text">${toFa(p.published_date)}</span>
                        </div>
                    </td>
                    <td class="ltr-text">${toFa(p.published_time)}</td>
                    <td>
                        <div class="date-stack">
                            ${mediaBadge(p.media_type)}
                            <span class="date-greg">${views}</span>
                        </div>
                    </td>
                    <td>
                        <div class="post-headline">${headline}</div>
                        <div class="post-snippet">${snippet}</div>
                    </td>
                    <td>
                        <a class="btn btn-outline btn-sm" href="${escapeHtml(
                            p.post_url
                        )}" target="_blank" rel="noopener noreferrer">مشاهده در سایت</a>
                    </td>
                    <td>
                        <div class="row-actions">
                            <button class="btn btn-ghost btn-sm" data-post-action="detail" data-id="${p.id}">جزئیات</button>
                            <button class="btn btn-danger-ghost btn-sm" data-post-action="delete" data-id="${p.id}">حذف</button>
                        </div>
                    </td>
                </tr>`;
            })
            .join("");
    }

    const info = $("#postsPaginationInfo");
    if (info) {
        const start = state.posts.total === 0 ? 0 : (state.posts.page - 1) * state.posts.pageSize + 1;
        const end = Math.min(state.posts.page * state.posts.pageSize, state.posts.total);
        info.textContent = `نمایش ${toFa(start)} تا ${toFa(end)} از مجموع ${toFa(
            state.posts.total
        )} مطلب`;
    }
    const indicator = $("#pageIndicator");
    if (indicator) {
        indicator.textContent = `صفحه ${toFa(state.posts.page)} از ${toFa(state.posts.totalPages)}`;
    }
    const prev = $("#btnPrevPage");
    const next = $("#btnNextPage");
    if (prev) prev.disabled = state.posts.page <= 1;
    if (next) next.disabled = state.posts.page >= state.posts.totalPages;

    syncSelectionUI();
}

async function loadPosts() {
    try {
        const query = buildQuery({
            site_id: state.filters.site_id,
            search: state.filters.search,
            date_from: state.filters.date_from,
            date_to: state.filters.date_to,
            media_type: state.filters.media_type,
            page: state.posts.page,
            page_size: state.posts.pageSize,
        });
        const data = await apiFetch(`/api/posts${query}`);
        state.posts.items = data.items || [];
        state.posts.total = data.total || 0;
        state.posts.page = data.page || 1;
        state.posts.totalPages = data.total_pages || 1;
        renderPosts();
    } catch (err) {
        showToast(err.message, "error");
    }
}

function openPostDetail(postId) {
    const post = state.posts.items.find((p) => String(p.id) === String(postId));
    if (!post) return;
    state.activeModalPost = post;

    $("#modalPostSiteTitle").textContent = post.site_title;
    $("#modalPostHeadline").textContent = post.title || "";
    $("#modalPostSeq").textContent = `#${toFa(post.item_seq)}`;
    $("#modalPostJalali").textContent = toFa(post.published_jalali_date);
    $("#modalPostGreg").textContent = toFa(post.published_date);
    $("#modalPostTime").textContent = toFa(post.published_time);
    $("#modalPostViews").textContent =
        post.views === null || post.views === undefined ? "—" : formatNumberFa(post.views);
    $("#modalPostContent").textContent = extractBodyWithoutTitle(post) || post.content || "[بدون متن]";
    const link = $("#modalPostUrl");
    link.textContent = post.post_url;
    link.href = post.post_url;

    $("#postDetailModal").classList.remove("hidden");
}

async function copyToClipboard(text) {
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
        } else {
            const area = document.createElement("textarea");
            area.value = text;
            area.style.position = "fixed";
            area.style.opacity = "0";
            document.body.appendChild(area);
            area.select();
            document.execCommand("copy");
            area.remove();
        }
        showToast("در حافظه کپی شد.", "success", 2200);
    } catch (_) {
        showToast("کپی کردن ممکن نشد. لطفاً متن را دستی انتخاب کنید.", "error");
    }
}

function initPostsEvents() {
    const form = $("#postFiltersForm");
    if (form) {
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            state.filters.site_id = $("#filterSite").value;
            state.filters.search = $("#filterSearch").value.trim();
            state.filters.date_from = $("#filterDateFrom").value.trim();
            state.filters.date_to = $("#filterDateTo").value.trim();
            state.filters.media_type = $("#filterMediaType").value;
            state.posts.page = 1;
            loadPosts();
        });
    }

    const clearBtn = $("#btnClearFilters");
    if (clearBtn) {
        clearBtn.addEventListener("click", () => {
            $("#filterSite").value = "";
            $("#filterSearch").value = "";
            $("#filterDateFrom").value = "";
            $("#filterDateTo").value = "";
            $("#filterMediaType").value = "all";
            state.filters = { site_id: "", search: "", date_from: "", date_to: "", media_type: "all" };
            state.posts.page = 1;
            loadPosts();
        });
    }

    const prev = $("#btnPrevPage");
    const next = $("#btnNextPage");
    if (prev) {
        prev.addEventListener("click", () => {
            if (state.posts.page > 1) {
                state.posts.page -= 1;
                loadPosts();
            }
        });
    }
    if (next) {
        next.addEventListener("click", () => {
            if (state.posts.page < state.posts.totalPages) {
                state.posts.page += 1;
                loadPosts();
            }
        });
    }

    const tbody = $("#postsTableBody");
    if (tbody) {
        tbody.addEventListener("click", async (event) => {
            const button = event.target.closest("button[data-post-action]");
            if (!button) return;
            const action = button.dataset.postAction;
            const id = button.dataset.id;

            if (action === "detail") {
                openPostDetail(id);
                return;
            }

            if (action === "delete") {
                if (!window.confirm("آیا این مطلب از آرشیو حذف شود؟")) return;
                try {
                    await apiFetch(`/api/posts/${id}`, { method: "DELETE" });
                    showToast("مطلب حذف شد.", "success");
                    await Promise.all([loadPosts(), loadOverview(), loadSites()]);
                } catch (err) {
                    showToast(err.message, "error");
                }
            }
        });
    }

    const copyText = $("#btnCopyPostText");
    if (copyText) {
        copyText.addEventListener("click", () => {
            if (state.activeModalPost) copyToClipboard(state.activeModalPost.content || "");
        });
    }
    const copyLink = $("#btnCopyPostLink");
    if (copyLink) {
        copyLink.addEventListener("click", () => {
            if (state.activeModalPost) copyToClipboard(state.activeModalPost.post_url || "");
        });
    }
}

/* ------------------------------ Exports ------------------------------ */

function currentFiltersForExport() {
    return {
        site_id: state.filters.site_id,
        search: state.filters.search,
        date_from: state.filters.date_from,
        date_to: state.filters.date_to,
        media_type: state.filters.media_type,
    };
}

function triggerDownload(url) {
    const link = document.createElement("a");
    link.href = url;
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
}

function initExports() {
    const quickExcel = $("#btnQuickExcel");
    if (quickExcel) {
        quickExcel.addEventListener("click", () => {
            triggerDownload("/api/exports/excel");
            showToast("فایل اکسل کامل در حال دانلود است...", "success");
        });
    }

    const quickSqlite = $("#btnQuickSqlite");
    if (quickSqlite) {
        quickSqlite.addEventListener("click", () => {
            triggerDownload("/api/exports/sqlite");
            showToast("فایل دیتابیس SQLite در حال دانلود است...", "success");
        });
    }

    const filteredExcel = $("#btnExportFilteredExcel");
    if (filteredExcel) {
        filteredExcel.addEventListener("click", () => {
            triggerDownload(`/api/exports/excel${buildQuery(currentFiltersForExport())}`);
            showToast("خروجی اکسل بر اساس فیلتر فعلی در حال دانلود است...", "success");
        });
    }

    const fullSqlite = $("#btnExportFullSqlite");
    if (fullSqlite) {
        fullSqlite.addEventListener("click", () => {
            triggerDownload("/api/exports/sqlite");
            showToast("کل دیتابیس در حال دانلود است...", "success");
        });
    }

    const filteredSqlite = $("#btnExportFilteredSqlite");
    if (filteredSqlite) {
        filteredSqlite.addEventListener("click", () => {
            const query = buildQuery(Object.assign({ filtered_only: "true" }, currentFiltersForExport()));
            triggerDownload(`/api/exports/sqlite${query}`);
            showToast("خروجی SQLite فیلترشده در حال دانلود است...", "success");
        });
    }
}

/* ------------------------------ Logs ------------------------------ */

function renderLogs(items) {
    const tbody = $("#logsTableBody");
    if (!tbody) return;

    if (!items.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty-cell">هنوز گزارشی از دریافت مطالب ثبت نشده است.</td></tr>';
        return;
    }

    tbody.innerHTML = items
        .map((log) => {
            const statusCls = log.status === "success" ? "badge-ok" : "badge-error";
            const statusText = STATUS_LABELS[log.status] || log.status;
            const sourceText = SOURCE_MODE_LABELS[log.source_mode] || log.source_mode;
            return `
            <tr>
                <td class="ltr-text">${toFa(log.created_display || log.created_at)}</td>
                <td>${escapeHtml(log.site_title)}</td>
                <td>${escapeHtml(TRIGGER_LABELS[log.trigger_type] || log.trigger_type)}</td>
                <td>${escapeHtml(sourceText)}</td>
                <td><strong>${formatNumberFa(log.new_saved_count)}</strong></td>
                <td class="ltr-text">${toFa(log.previous_last_item_seq)} &rarr; ${toFa(log.new_last_item_seq)}</td>
                <td>${toFa(log.duration_ms)} ms</td>
                <td>
                    <span class="badge ${statusCls}">${escapeHtml(statusText)}</span>
                    <div class="error-hint" style="color:#475569" title="${escapeHtml(log.message || "")}">
                        ${escapeHtml(String(log.message || "").slice(0, 110))}${String(log.message || "").length > 110 ? "…" : ""}
                    </div>
                </td>
            </tr>`;
        })
        .join("");
}

async function loadLogs() {
    try {
        const data = await apiFetch("/api/logs?limit=150");
        renderLogs(data.items || []);
    } catch (err) {
        showToast(err.message, "error");
    }
}

function initLogsEvents() {
    const refresh = $("#btnRefreshLogs");
    if (refresh) refresh.addEventListener("click", () => loadLogs());

    const clear = $("#btnClearLogs");
    if (clear) {
        clear.addEventListener("click", async () => {
            if (!window.confirm("تمام تاریخچه گزارش‌ها پاک شود؟")) return;
            try {
                await apiFetch("/api/logs", { method: "DELETE" });
                showToast("تاریخچه گزارش‌ها پاک شد.", "success");
                loadLogs();
            } catch (err) {
                showToast(err.message, "error");
            }
        });
    }
}

/* ------------------- روش‌های اتصال اختصاصی وب‌سایت‌ها (پروکسی per-site) ------------------- */

const CONNECTION_KIND_LABELS = {
    proxy: "پروکسی",
    direct: "اتصال مستقیم",
};

/** پرکردن هر <select> که فهرست روش‌های اتصال را نشان می‌دهد */
function fillConnectionProfileSelect(select, { placeholder = "حالت عمومی برنامه" } = {}) {
    if (!select) return;
    const previous = select.value;
    select.innerHTML =
        `<option value="">${escapeHtml(placeholder)}</option>` +
        state.connectionProfiles
            .map((p) => {
                const label =
                    p.kind === "direct"
                        ? `${p.name} — اتصال مستقیم (بدون پروکسی)`
                        : `${p.name} — ${p.connection_label || "پروکسی"}`;
                const suffix = p.is_enabled ? "" : " (غیرفعال)";
                return `<option value="${p.id}">${escapeHtml(label)}${suffix}</option>`;
            })
            .join("");
    if (previous && state.connectionProfiles.some((p) => String(p.id) === String(previous))) {
        select.value = previous;
    }
}

function renderConnectionSummary() {
    const summary = state.connectionSummary || {};
    const set = (selector, value) => {
        const el = $(selector);
        if (el) el.textContent = formatNumberFa(value || 0);
    };
    set("#cpSummaryProfiles", summary.profiles);
    set("#cpSummaryAssigned", summary.assigned_sites);
    set("#cpSummaryManual", summary.manual_proxy_sites);
    set("#cpSummaryGeneral", summary.general_mode_sites);
}

function renderConnectionProfilesTable() {
    const tbody = $("#connectionProfilesBody");
    if (!tbody) return;

    if (!state.connectionProfiles.length) {
        tbody.innerHTML = `
            <tr>
                <td colspan="7" class="empty-cell">
                    هنوز روش اتصال اختصاصی تعریف نکرده‌اید؛ همه وب‌سایت‌ها از تنظیمات عمومی بالای همین صفحه استفاده می‌کنند.
                </td>
            </tr>`;
        return;
    }

    tbody.innerHTML = state.connectionProfiles
        .map((p) => {
            const kindLabel = CONNECTION_KIND_LABELS[p.kind] || p.kind;
            const kindBadge =
                p.kind === "direct"
                    ? '<span class="badge badge-paused">اتصال مستقیم</span>'
                    : '<span class="badge badge-proxy">پروکسی</span>';
            const address = p.kind === "direct" ? "—" : `<code class="ltr-text">${escapeHtml(p.proxy_url || "")}</code>`;

            const extras = [];
            if (p.verify_ssl === true) extras.push("SSL همیشه بررسی شود");
            if (p.verify_ssl === false) extras.push("بدون بررسی SSL");
            if (p.request_timeout) extras.push(`مهلت ${toFa(p.request_timeout)} ثانیه`);
            extras.push(p.fallback_direct ? "با مسیر جایگزین مستقیم" : "فقط از طریق پروکسی");
            if (!p.is_enabled) extras.push("غیرفعال");

            const assigned = p.assigned_site_list || [];
            const assignedHtml = assigned.length
                ? `<div class="cp-assigned-count"><strong>${formatNumberFa(assigned.length)}</strong> وب‌سایت</div>
                   <div class="cp-assigned-names" title="${escapeHtml(
                       assigned.map((s) => s.title).join("، ")
                   )}">${escapeHtml(assigned.map((s) => s.title).join("، "))}</div>`
                : '<span class="muted">به هیچ وب‌سایتی نسبت داده نشده</span>';

            let testHtml = '<span class="muted">تست نشده</span>';
            if (p.last_test_at) {
                const badge =
                    p.last_test_ok === true
                        ? '<span class="badge badge-ok">موفق</span>'
                        : '<span class="badge badge-error">ناموفق</span>';
                testHtml = `${badge}<div class="error-hint" title="${escapeHtml(
                    p.last_test_message || ""
                )}">${escapeHtml(String(p.last_test_message || "").slice(0, 70))}</div>
                <div class="muted cp-test-time">${escapeHtml(p.last_test_display || "")}</div>`;
            }

            return `
            <tr>
                <td>
                    <div class="ch-title-wrap">
                        <span class="ch-title">${escapeHtml(p.name)}</span>
                        ${p.notes ? `<span class="ch-link">${escapeHtml(p.notes)}</span>` : ""}
                    </div>
                </td>
                <td>${kindBadge}<div class="muted">${escapeHtml(kindLabel)}</div></td>
                <td>${address}</td>
                <td><div class="cp-extras">${extras.map((x) => escapeHtml(x)).join(" · ")}</div></td>
                <td>${assignedHtml}</td>
                <td>${testHtml}</td>
                <td>
                    <div class="row-actions">
                        <button class="btn btn-outline btn-sm" data-cp-action="test" data-id="${p.id}">تست اتصال</button>
                        <button class="btn btn-ghost btn-sm" data-cp-action="edit" data-id="${p.id}">ویرایش</button>
                        <button class="btn btn-ghost btn-sm" data-cp-action="assign" data-id="${p.id}">انتخاب سایت‌ها</button>
                        <button class="btn btn-danger-ghost btn-sm" data-cp-action="delete" data-id="${p.id}">حذف</button>
                    </div>
                </td>
            </tr>`;
        })
        .join("");
}

function connectionSitesForList() {
    if (Array.isArray(state.connectionSites) && state.connectionSites.length) return state.connectionSites;
    return state.sites.map((s) => ({
        id: s.id,
        title: s.title,
        domain: s.domain,
        canonical_url: s.canonical_url,
        is_active: s.is_active,
        is_demo: s.is_demo,
        posts_count: s.posts_count,
        connection_profile_id: s.connection_profile_id,
        connection_profile_name: s.connection_profile_name,
        connection_label: s.connection_label,
        proxy_url: s.proxy_url,
    }));
}

function renderConnectionSiteList() {
    const box = $("#cpSiteList");
    if (!box) return;

    const needle = normalizeSearchText(state.connectionSiteFilter || "");
    const all = connectionSitesForList();
    const visible = needle
        ? all.filter((s) =>
              normalizeSearchText(`${s.title || ""} ${s.domain || ""} ${s.canonical_url || ""}`).includes(needle)
          )
        : all;

    if (!all.length) {
        box.innerHTML =
            '<div class="empty-cell">هنوز وب‌سایتی در برنامه ثبت نشده است. ابتدا از زبانه «۱. وب‌سایت‌ها» یک وب‌سایت اضافه کنید.</div>';
        return;
    }
    if (!visible.length) {
        box.innerHTML = '<div class="empty-cell">هیچ وب‌سایتی مطابق با این جستجو پیدا نشد.</div>';
        return;
    }

    box.innerHTML = visible
        .map((s) => {
            const checked = state.connectionSelectedSites.has(String(s.id)) ? "checked" : "";
            const hasCustom = Boolean(s.connection_profile_id) || Boolean(s.proxy_url);
            const badge = s.connection_profile_name
                ? `<span class="badge badge-proxy" title="${escapeHtml(s.connection_label || "")}">${escapeHtml(
                      s.connection_profile_name
                  )}</span>`
                : hasCustom
                ? `<span class="badge badge-proxy badge-proxy-manual" title="${escapeHtml(
                      s.connection_label || ""
                  )}">پروکسی دستی</span>`
                : '<span class="badge badge-pending">حالت عمومی</span>';
            const demo = s.is_demo ? '<span class="badge badge-demo">نمونه</span>' : "";
            return `
            <label class="cp-site-row">
                <input type="checkbox" data-cp-site="${s.id}" ${checked} />
                <span class="cp-site-main">
                    <span class="cp-site-title">${escapeHtml(s.title || "")} ${demo}</span>
                    <span class="cp-site-url ltr-text">${escapeHtml(s.canonical_url || "")}</span>
                </span>
                <span class="cp-site-state">${badge}</span>
            </label>`;
        })
        .join("");
}

function updateConnectionSelectionUI() {
    const ids = Array.from(state.connectionSelectedSites);
    const countEl = $("#cpSelectedCount");
    if (countEl) countEl.textContent = formatNumberFa(ids.length);

    const namesEl = $("#cpSelectedNames");
    if (namesEl) {
        const sites = connectionSitesForList();
        const names = ids
            .map((id) => (sites.find((s) => String(s.id) === String(id)) || {}).title)
            .filter(Boolean);
        const shown = names.slice(0, 4).join("، ");
        namesEl.textContent = names.length
            ? `${shown}${names.length > 4 ? ` و ${toFa(names.length - 4)} مورد دیگر` : ""}`
            : "";
    }

    const applyBtn = $("#btnApplyConnectionProfile");
    if (applyBtn) applyBtn.disabled = ids.length === 0;
}

function resetConnectionProfileForm() {
    state.editingProfileId = null;
    const form = $("#connectionProfileForm");
    if (form) form.reset();
    $("#cpEditId").value = "";
    $("#cpName").value = "";
    $("#cpHost").value = "";
    $("#cpPort").value = "";
    $("#cpUser").value = "";
    $("#cpPass").value = "";
    $("#cpNotes").value = "";
    $("#cpTimeout").value = "";
    $("#cpVerifySsl").value = "inherit";
    $("#cpFallback").checked = true;
    $("#cpEnabled").checked = true;
    const proxyRadio = document.querySelector('input[name="cpKind"][value="proxy"]');
    if (proxyRadio) proxyRadio.checked = true;
    toggleConnectionKindFields();

    const title = $("#cpFormTitle");
    if (title) title.textContent = "۱) تعریف روش اتصال جدید";
    const saveBtn = $("#btnSaveConnectionProfile");
    if (saveBtn) saveBtn.textContent = "ذخیره روش اتصال";
    const cancelBtn = $("#btnCancelConnectionProfileEdit");
    if (cancelBtn) cancelBtn.classList.add("hidden");
    const msg = $("#cpFormMessage");
    if (msg) msg.textContent = "";
}

function toggleConnectionKindFields() {
    const directRadio = document.querySelector('input[name="cpKind"][value="direct"]');
    const box = $("#cpProxyFields");
    if (!box) return;
    const isDirect = Boolean(directRadio && directRadio.checked);
    box.classList.toggle("hidden", isDirect);
}

function fillConnectionProfileForm(profile) {
    if (!profile) return;
    state.editingProfileId = profile.id;
    $("#cpEditId").value = profile.id;
    $("#cpName").value = profile.name || "";
    const kindRadio = document.querySelector(`input[name="cpKind"][value="${profile.kind === "direct" ? "direct" : "proxy"}"]`);
    if (kindRadio) kindRadio.checked = true;
    toggleConnectionKindFields();
    $("#cpType").value = profile.proxy_type || "socks5";
    $("#cpHost").value = profile.proxy_host || "";
    $("#cpPort").value = profile.proxy_port || "";
    $("#cpUser").value = profile.proxy_username || "";
    $("#cpPass").value = profile.proxy_password_set ? "********" : "";
    $("#cpVerifySsl").value =
        profile.verify_ssl === true ? "on" : profile.verify_ssl === false ? "off" : "inherit";
    $("#cpTimeout").value = profile.request_timeout ? String(profile.request_timeout) : "";
    $("#cpFallback").checked = profile.fallback_direct !== false;
    $("#cpNotes").value = profile.notes || "";
    $("#cpEnabled").checked = profile.is_enabled !== false;

    const title = $("#cpFormTitle");
    if (title) title.textContent = `۱) ویرایش روش اتصال «${profile.name}»`;
    const saveBtn = $("#btnSaveConnectionProfile");
    if (saveBtn) saveBtn.textContent = "ذخیره تغییرات";
    const cancelBtn = $("#btnCancelConnectionProfileEdit");
    if (cancelBtn) cancelBtn.classList.remove("hidden");
    const msg = $("#cpFormMessage");
    if (msg) msg.textContent = "تغییرات از اولین دریافت بعدی وب‌سایت‌های متصل اعمال می‌شود.";
}

function collectConnectionProfilePayload() {
    const kindRadio = document.querySelector('input[name="cpKind"]:checked');
    const kind = kindRadio ? kindRadio.value : "proxy";
    const verifyMode = $("#cpVerifySsl").value;
    const timeoutRaw = $("#cpTimeout").value.trim();

    return {
        name: $("#cpName").value.trim(),
        kind,
        proxy_type: $("#cpType").value,
        proxy_host: $("#cpHost").value.trim(),
        proxy_port: $("#cpPort").value.trim(),
        proxy_username: $("#cpUser").value.trim(),
        proxy_password: $("#cpPass").value,
        verify_ssl: verifyMode === "on" ? true : verifyMode === "off" ? false : null,
        request_timeout: timeoutRaw ? Number(timeoutRaw) : null,
        fallback_direct: $("#cpFallback").checked,
        notes: $("#cpNotes").value.trim(),
        is_enabled: $("#cpEnabled").checked,
    };
}

function showAssignResult(message, ok) {
    const box = $("#cpAssignResult");
    if (!box) return;
    box.className = `preview-box ${ok ? "safe" : "danger"}`;
    box.textContent = message;
}

function showTestResult(message, ok) {
    const box = $("#cpTestResultBox");
    if (!box) return;
    box.className = `preview-box ${ok ? "safe" : "danger"}`;
    box.innerHTML = message;
}

async function loadConnectionProfiles() {
    try {
        const data = await apiFetch("/api/connection-profiles");
        state.connectionProfiles = data.items || [];
        state.connectionSummary = data.summary || {};
        state.connectionSites = Array.isArray(data.sites) ? data.sites : [];

        renderConnectionSummary();
        renderConnectionProfilesTable();
        fillConnectionProfileSelect($("#cpAssignProfile"), {
            placeholder: "حالت عمومی برنامه (بدون پروکسی اختصاصی)",
        });
        fillConnectionProfileSelect($("#probeConnectionProfile"), {
            placeholder: "حالت عمومی برنامه",
        });
        renderConnectionSiteList();
        updateConnectionSelectionUI();

        if (
            state.editingProfileId &&
            !state.connectionProfiles.some((p) => String(p.id) === String(state.editingProfileId))
        ) {
            resetConnectionProfileForm();
        }
    } catch (err) {
        showToast(err.message, "error");
    }
}

async function saveConnectionProfile(button) {
    const payload = collectConnectionProfilePayload();
    if (!payload.name) {
        showToast("نام روش اتصال را وارد کنید.", "error");
        return;
    }
    if (payload.kind === "proxy" && (!payload.proxy_host || !payload.proxy_port)) {
        showToast("برای روش اتصال پروکسی، وارد کردن آدرس میزبان و پورت الزامی است.", "error");
        return;
    }

    const editingId = state.editingProfileId;
    setBusy(button, true, "در حال ذخیره...");
    try {
        const res = editingId
            ? await apiFetch(`/api/connection-profiles/${editingId}`, { method: "PUT", body: payload })
            : await apiFetch("/api/connection-profiles", { method: "POST", body: payload });
        showToast(res.message || "روش اتصال ذخیره شد.", "success", 6000);
        resetConnectionProfileForm();
        await loadConnectionProfiles();
    } catch (err) {
        showToast(err.message, "error");
    } finally {
        setBusy(button, false);
    }
}

async function testConnectionProfile(profileId, button) {
    const profile = state.connectionProfiles.find((p) => String(p.id) === String(profileId));
    const assigned = (profile && profile.assigned_site_list) || [];
    const target = assigned.length ? assigned[0].canonical_url : "";

    setBusy(button, true, "تست اتصال...");
    showTestResult("در حال تست اتصال از طریق این روش اتصال ...", true);
    try {
        const res = await apiFetch(`/api/connection-profiles/${profileId}/test`, {
            method: "POST",
            body: target ? { target_url: target } : {},
        });
        const targetLine = `آدرس تست: <code class="ltr-text">${escapeHtml(res.target_url || "")}</code>`;
        const ipLine = res.exit_ip
            ? ` — IP خروجی: <code class="ltr-text">${escapeHtml(res.exit_ip)}</code>`
            : "";
        const timeLine = `زمان پاسخ: ${formatNumberFa(res.elapsed_ms)} میلی‌ثانیه`;
        showTestResult(
            `<strong>${res.ok ? "اتصال برقرار است." : "اتصال برقرار نشد."}</strong> ${escapeHtml(
                res.message || ""
            )}<br>${targetLine}${ipLine}<br>${timeLine}`,
            Boolean(res.ok)
        );
        showToast(res.message || (res.ok ? "تست اتصال موفق بود." : "تست اتصال ناموفق بود."), res.ok ? "success" : "error", 6000);
        await loadConnectionProfiles();
    } catch (err) {
        showTestResult(err.message, false);
        showToast(err.message, "error");
    } finally {
        setBusy(button, false);
    }
}

async function deleteConnectionProfile(profileId, button) {
    const profile = state.connectionProfiles.find((p) => String(p.id) === String(profileId));
    const assignedCount = (profile && (profile.assigned_site_list || []).length) || 0;
    const confirmed = window.confirm(
        `روش اتصال «${profile ? profile.name : profileId}» حذف شود؟\n\n` +
            (assignedCount
                ? `${toFa(assignedCount)} وب‌سایت متصل به آن، به حالت عمومی برنامه بازمی‌گردند.`
                : "این روش اتصال به هیچ وب‌سایتی نسبت داده نشده است.")
    );
    if (!confirmed) return;

    setBusy(button, true, "در حال حذف...");
    try {
        const res = await apiFetch(`/api/connection-profiles/${profileId}`, { method: "DELETE" });
        showToast(res.message || "روش اتصال حذف شد.", "success", 6000);
        if (String(state.editingProfileId) === String(profileId)) resetConnectionProfileForm();
        await Promise.all([loadConnectionProfiles(), loadSites(), loadOverview()]);
    } catch (err) {
        showToast(err.message, "error");
    } finally {
        setBusy(button, false);
    }
}

async function applyConnectionProfileToSelected(button) {
    const ids = Array.from(state.connectionSelectedSites);
    if (!ids.length) {
        showToast("ابتدا دست‌کم یک وب‌سایت را از فهرست انتخاب کنید.", "error");
        return;
    }

    const raw = ($("#cpAssignProfile") && $("#cpAssignProfile").value) || "";
    const profileId = raw ? Number(raw) : null;
    const profile = state.connectionProfiles.find((p) => String(p.id) === String(raw));

    const confirmed = window.confirm(
        profileId
            ? `روش اتصال «${profile ? profile.name : raw}» روی ${toFa(ids.length)} وب‌سایت انتخاب‌شده اعمال شود؟\n\n` +
              "از دریافت بعدی، فقط این وب‌سایت‌ها از این پروکسی استفاده می‌کنند و بقیه وب‌سایت‌ها در حالت عمومی باقی می‌مانند."
            : `${toFa(ids.length)} وب‌سایت انتخاب‌شده به «حالت عمومی برنامه» بازگردانده شوند؟`
    );
    if (!confirmed) return;

    setBusy(button, true, "در حال اعمال...");
    try {
        const res = await apiFetch("/api/connection-profiles/assign", {
            method: "POST",
            body: {
                profile_id: profileId,
                site_ids: ids.map((id) => Number(id)),
                clear_manual_proxy: true,
            },
        });
        showAssignResult(res.message || "روش اتصال اعمال شد.", true);
        showToast(res.message || "روش اتصال اعمال شد.", "success", 6000);
        state.connectionSelectedSites.clear();
        await Promise.all([loadConnectionProfiles(), loadSites(), loadOverview()]);
    } catch (err) {
        showAssignResult(err.message, false);
        showToast(err.message, "error");
    } finally {
        setBusy(button, false);
    }
}

function initConnectionProfileEvents() {
    const form = $("#connectionProfileForm");
    if (form) {
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            saveConnectionProfile($("#btnSaveConnectionProfile"));
        });
    }

    $$('input[name="cpKind"]').forEach((radio) =>
        radio.addEventListener("change", toggleConnectionKindFields)
    );

    const cancelBtn = $("#btnCancelConnectionProfileEdit");
    if (cancelBtn) cancelBtn.addEventListener("click", resetConnectionProfileForm);

    const reloadBtn = $("#btnReloadConnectionProfiles");
    if (reloadBtn) {
        reloadBtn.addEventListener("click", async () => {
            setBusy(reloadBtn, true, "بروزرسانی...");
            await Promise.all([loadConnectionProfiles(), loadSites()]);
            setBusy(reloadBtn, false);
            showToast("فهرست روش‌های اتصال بروزرسانی شد.", "success", 2500);
        });
    }

    const searchInput = $("#cpSiteSearch");
    if (searchInput) {
        searchInput.addEventListener("input", () => {
            state.connectionSiteFilter = searchInput.value;
            renderConnectionSiteList();
        });
    }

    const listBox = $("#cpSiteList");
    if (listBox) {
        listBox.addEventListener("change", (event) => {
            const input = event.target;
            if (!input || !input.dataset || !input.dataset.cpSite) return;
            const siteId = String(input.dataset.cpSite);
            if (input.checked) state.connectionSelectedSites.add(siteId);
            else state.connectionSelectedSites.delete(siteId);
            updateConnectionSelectionUI();
        });
    }

    const selectAll = $("#btnCpSelectAll");
    if (selectAll) {
        selectAll.addEventListener("click", () => {
            connectionSitesForList()
                .filter((s) => {
                    const needle = normalizeSearchText(state.connectionSiteFilter || "");
                    if (!needle) return true;
                    return normalizeSearchText(
                        `${s.title || ""} ${s.domain || ""} ${s.canonical_url || ""}`
                    ).includes(needle);
                })
                .forEach((s) => state.connectionSelectedSites.add(String(s.id)));
            renderConnectionSiteList();
            updateConnectionSelectionUI();
        });
    }

    const selectNone = $("#btnCpSelectNone");
    if (selectNone) {
        selectNone.addEventListener("click", () => {
            state.connectionSelectedSites.clear();
            renderConnectionSiteList();
            updateConnectionSelectionUI();
        });
    }

    const selectCustom = $("#btnCpSelectCustom");
    if (selectCustom) {
        selectCustom.addEventListener("click", () => {
            state.connectionSelectedSites.clear();
            connectionSitesForList()
                .filter((s) => Boolean(s.connection_profile_id) || Boolean(s.proxy_url))
                .forEach((s) => state.connectionSelectedSites.add(String(s.id)));
            renderConnectionSiteList();
            updateConnectionSelectionUI();
            if (!state.connectionSelectedSites.size) {
                showToast("هیچ وب‌سایتی روش اتصال اختصاصی ندارد.", "info");
            }
        });
    }

    const applyBtn = $("#btnApplyConnectionProfile");
    if (applyBtn) applyBtn.addEventListener("click", () => applyConnectionProfileToSelected(applyBtn));

    const profilesBody = $("#connectionProfilesBody");
    if (profilesBody) {
        profilesBody.addEventListener("click", (event) => {
            const button = event.target.closest("button[data-cp-action]");
            if (!button) return;
            const action = button.dataset.cpAction;
            const profileId = button.dataset.id;
            const profile = state.connectionProfiles.find((p) => String(p.id) === String(profileId));

            if (action === "edit") {
                fillConnectionProfileForm(profile);
                const card = document.querySelector(".connection-card");
                if (card) card.scrollIntoView({ behavior: "smooth", block: "start" });
                return;
            }
            if (action === "test") {
                testConnectionProfile(profileId, button);
                return;
            }
            if (action === "assign") {
                const select = $("#cpAssignProfile");
                if (select) select.value = String(profileId);
                if (profile) {
                    state.connectionSelectedSites.clear();
                    (profile.assigned_site_list || []).forEach((s) =>
                        state.connectionSelectedSites.add(String(s.id))
                    );
                }
                renderConnectionSiteList();
                updateConnectionSelectionUI();
                const listBoxEl = $("#cpSiteList");
                if (listBoxEl) listBoxEl.scrollIntoView({ behavior: "smooth", block: "center" });
                showToast(
                    profile && (profile.assigned_site_list || []).length
                        ? "وب‌سایت‌های متصل به این روش اتصال، در فهرست انتخاب شدند."
                        : "اکنون وب‌سایت‌های دلخواه را از فهرست انتخاب کنید.",
                    "info",
                    5000
                );
                return;
            }
            if (action === "delete") {
                deleteConnectionProfile(profileId, button);
            }
        });
    }
}

/* ------------------------------ Settings ------------------------------ */

async function loadSettings() {
    try {
        const data = await apiFetch("/api/settings");
        const s = data.settings || {};

        $("#settingCollectorMode").value = s.collector_mode || "auto";
        $("#settingTimezone").value = s.timezone || "Asia/Tehran";
        $("#settingDefaultLimit").value = s.default_initial_limit || 10;
        $("#settingFetchFullContent").checked = Boolean(s.fetch_full_content);
        $("#settingVerifySsl").checked = Boolean(s.verify_ssl);
        $("#settingRequestTimeout").value = s.request_timeout || 25;
        $("#settingUserAgent").value = s.custom_user_agent || "";
        $("#settingCustomCookie").value = s.custom_cookie_set ? "********" : "";

        $("#settingProxyEnabled").checked = Boolean(s.proxy_enabled);
        $("#settingProxyType").value = s.proxy_type || "socks5";
        $("#settingProxyHost").value = s.proxy_host || "";
        $("#settingProxyPort").value = s.proxy_port || "";
        $("#settingProxyUser").value = s.proxy_username || "";
        $("#settingProxyPass").value = s.proxy_password_set ? "********" : "";
        toggleProxyFields();
    } catch (err) {
        showToast(err.message, "error");
    }

    // روش‌های اتصال اختصاصی وب‌سایت‌ها (پروکسی per-site) در همان زبانه تنظیمات
    await loadConnectionProfiles();
}

function toggleProxyFields() {
    const enabled = $("#settingProxyEnabled").checked;
    const box = $("#proxyFieldsBox");
    if (!box) return;
    box.style.opacity = enabled ? "1" : "0.55";
    box.style.pointerEvents = enabled ? "auto" : "none";
}

function collectSettingsPayload() {
    return {
        collector_mode: $("#settingCollectorMode").value,
        timezone: $("#settingTimezone").value,
        default_initial_limit: Number($("#settingDefaultLimit").value) || 10,
        fetch_full_content: $("#settingFetchFullContent").checked,
        verify_ssl: $("#settingVerifySsl").checked,
        request_timeout: Number($("#settingRequestTimeout").value) || 25,
        custom_user_agent: $("#settingUserAgent").value.trim(),
        custom_cookie: $("#settingCustomCookie").value,
        proxy_enabled: $("#settingProxyEnabled").checked,
        proxy_type: $("#settingProxyType").value,
        proxy_host: $("#settingProxyHost").value.trim(),
        proxy_port: $("#settingProxyPort").value.trim(),
        proxy_username: $("#settingProxyUser").value.trim(),
        proxy_password: $("#settingProxyPass").value,
    };
}

function initSettingsEvents() {
    const proxyToggle = $("#settingProxyEnabled");
    if (proxyToggle) proxyToggle.addEventListener("change", toggleProxyFields);

    const form = $("#settingsForm");
    if (form) {
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const button = form.querySelector('button[type="submit"]');
            setBusy(button, true, "در حال ذخیره...");
            try {
                await apiFetch("/api/settings", {
                    method: "PUT",
                    body: collectSettingsPayload(),
                });
                showToast("تنظیمات با موفقیت ذخیره شد.", "success");
                await loadOverview();
            } catch (err) {
                showToast(err.message, "error");
            } finally {
                setBusy(button, false);
            }
        });
    }

    const saveAdv = $("#btnSaveAdvancedHttp");
    if (saveAdv) {
        saveAdv.addEventListener("click", async () => {
            setBusy(saveAdv, true, "ذخیره...");
            try {
                await apiFetch("/api/settings", {
                    method: "PUT",
                    body: collectSettingsPayload(),
                });
                showToast("تنظیمات پیشرفته ذخیره شد.", "success");
            } catch (err) {
                showToast(err.message, "error");
            } finally {
                setBusy(saveAdv, false);
            }
        });
    }

    const probeBtn = $("#btnProbeSite");
    if (probeBtn) {
        probeBtn.addEventListener("click", async () => {
            const rawUrl = $("#probeSiteInput").value.trim();
            const box = $("#probeResultBox");
            if (!rawUrl) {
                showToast("ابتدا آدرس وب‌سایت را برای بررسی وارد کنید.", "error");
                return;
            }
            box.classList.remove("hidden");
            box.className = "preview-box";
            box.textContent = "در حال اتصال و بررسی ساختار وب‌سایت ...";
            setBusy(probeBtn, true, "بررسی...");
            try {
                const profileSelect = $("#probeConnectionProfile");
                const profileId = profileSelect && profileSelect.value ? Number(profileSelect.value) : null;
                const res = await apiFetch("/api/sites/probe", {
                    method: "POST",
                    body: profileId
                        ? { site_input: rawUrl, connection_profile_id: profileId }
                        : { site_input: rawUrl },
                });
                const modeLabel = SOURCE_MODE_LABELS[res.source_mode] || res.source_mode;
                const samples = (res.sample_titles || [])
                    .slice(0, 3)
                    .map((t) => `<li>${escapeHtml(t)}</li>`)
                    .join("");
                box.className = "preview-box safe";
                box.innerHTML =
                    `<strong>اتصال موفق!</strong> عنوان: ${escapeHtml(res.site_title)}<br>` +
                    `موتور تشخیص‌داده‌شده: <strong>${escapeHtml(modeLabel)}</strong> — ` +
                    `${formatNumberFa(res.found_posts_count)} مطلب در صفحه اول یافت شد.` +
                    (res.connection_label
                        ? `<br>روش اتصال استفاده‌شده: <strong>${escapeHtml(res.connection_label)}</strong>`
                        : "") +
                    (res.discovered_feed_url
                        ? `<br>فید کشف‌شده: <code class="ltr-text">${escapeHtml(res.discovered_feed_url)}</code>`
                        : "") +
                    (samples ? `<ul class="backup-list">${samples}</ul>` : "");
            } catch (err) {
                box.className = "preview-box danger";
                box.textContent = err.message;
            } finally {
                setBusy(probeBtn, false);
            }
        });
    }

    const seed = $("#btnSeedDemo");
    if (seed) seed.addEventListener("click", () => runSeedDemo(seed));

    const seedQuick = $("#btnSeedDemoQuick");
    if (seedQuick) seedQuick.addEventListener("click", () => runSeedDemo(seedQuick));

    const clearDemo = $("#btnClearDemo");
    if (clearDemo) {
        clearDemo.addEventListener("click", async () => {
            if (!window.confirm("تمام وب‌سایت‌ها و مطالب آزمایشی (نمونه) حذف شوند؟")) return;
            setBusy(clearDemo, true, "پاک‌سازی...");
            try {
                const res = await apiFetch("/api/demo", { method: "DELETE" });
                showToast(res.message || "داده‌های آزمایشی پاک شد.", "success");
                await Promise.all([loadSites(), loadOverview(), loadPosts(), loadLogs()]);
            } catch (err) {
                showToast(err.message, "error");
            } finally {
                setBusy(clearDemo, false);
            }
        });
    }
}

async function runSeedDemo(button) {
    setBusy(button, true, "در حال ساخت داده نمونه...");
    try {
        const res = await apiFetch("/api/demo/seed", { method: "POST" });
        showToast(res.message || "داده‌های نمونه بارگذاری شد.", "success", 6000);
        await Promise.all([loadSites(), loadOverview(), loadPosts(), loadLogs()]);
    } catch (err) {
        showToast(err.message, "error");
    } finally {
        setBusy(button, false);
    }
}

/* ------------------------------ Post Selection ------------------------------ */

function syncSelectionUI() {
    const count = state.selectedPostIds.size;

    const toolbar = $("#selectionToolbar");
    if (toolbar) toolbar.classList.toggle("hidden", count === 0);

    const countEl = $("#selectionCount");
    if (countEl) countEl.textContent = toFa(count);

    const cleanupCount = $("#cleanupSelectedCount");
    if (cleanupCount) cleanupCount.textContent = toFa(count);

    const selectedPreview = $("#selectedPreviewBox");
    if (selectedPreview) {
        if (count === 0) {
            selectedPreview.className = "preview-box";
            selectedPreview.textContent = "هیچ مطلبی انتخاب نشده است.";
        } else {
            selectedPreview.className = "preview-box danger";
            selectedPreview.innerHTML = `<strong>${toFa(count)} مطلب</strong> برای حذف انتخاب شده است.`;
        }
    }

    const header = $("#selectAllPosts");
    if (header) {
        const pageIds = state.posts.items.map((p) => Number(p.id));
        const selectedOnPage = pageIds.filter((id) => state.selectedPostIds.has(id)).length;
        header.checked = pageIds.length > 0 && selectedOnPage === pageIds.length;
        header.indeterminate = selectedOnPage > 0 && selectedOnPage < pageIds.length;
    }

    const hint = $("#selectionHint");
    if (hint) {
        hint.textContent =
            count > 0
                ? "— می‌توانید فقط موارد تیک‌خورده را حذف کنید"
                : "— برای حذف دستی، ردیف‌ها را تیک بزنید";
    }
}

function togglePostSelection(postId, selected) {
    const id = Number(postId);
    if (selected) state.selectedPostIds.add(id);
    else state.selectedPostIds.delete(id);

    const input = document.querySelector(`input[data-post-select="${id}"]`);
    if (input) {
        input.checked = selected;
        const row = input.closest("tr");
        if (row) row.classList.toggle("row-selected", selected);
    }
    syncSelectionUI();
}

function selectAllOnPage(selected) {
    state.posts.items.forEach((p) => {
        const id = Number(p.id);
        if (selected) state.selectedPostIds.add(id);
        else state.selectedPostIds.delete(id);
    });
    renderPosts();
}

async function selectAllMatchingFilter() {
    try {
        const query = buildQuery({
            site_id: state.filters.site_id,
            search: state.filters.search,
            date_from: state.filters.date_from,
            date_to: state.filters.date_to,
            media_type: state.filters.media_type,
        });
        const data = await apiFetch(`/api/posts/ids${query}`);
        (data.ids || []).forEach((id) => state.selectedPostIds.add(Number(id)));
        renderPosts();
        showToast(`${toFa(data.total || 0)} مطلب از تمام نتایج فیلتر انتخاب شد.`, "success", 5000);
    } catch (err) {
        showToast(err.message, "error");
    }
}

function clearSelection() {
    state.selectedPostIds.clear();
    renderPosts();
}

async function deleteSelectedPosts(button) {
    const ids = Array.from(state.selectedPostIds);
    if (!ids.length) {
        showToast("هیچ مطلبی انتخاب نشده است.", "error");
        return;
    }

    const confirmed = window.confirm(
        `آیا از حذف ${toFa(ids.length)} مطلب انتخاب‌شده مطمئن هستید؟\n\nاین عملیات قابل بازگشت نیست.`
    );
    if (!confirmed) return;

    setBusy(button, true, "در حال حذف...");
    try {
        const result = await apiFetch("/api/posts/bulk-delete", {
            method: "POST",
            body: { post_ids: ids, reset_site_cursors: true },
        });
        showToast(result.message || "حذف انجام شد.", "success", 6000);
        state.selectedPostIds.clear();
        await Promise.all([
            loadSites(),
            loadOverview(),
            loadPosts(),
            loadLogs(),
            loadCleanupTab(),
        ]);
    } catch (err) {
        showToast(err.message, "error", 7000);
    } finally {
        setBusy(button, false);
    }
}

/* ------------------------------ Cleanup / Bulk Delete ------------------------------ */

function collectDeleteScope() {
    const siteValue = ($("#deleteScopeSite") || {}).value || "";
    const mediaValue = ($("#deleteScopeMedia") || {}).value || "all";
    return {
        site_id: siteValue ? Number(siteValue) : null,
        date_from: (($("#deleteScopeDateFrom") || {}).value || "").trim() || null,
        date_to: (($("#deleteScopeDateTo") || {}).value || "").trim() || null,
        media_type: mediaValue === "all" ? null : mediaValue,
        search: (($("#deleteScopeSearch") || {}).value || "").trim() || null,
        reset_site_cursors: Boolean(($("#deleteResetCursors") || {}).checked),
    };
}

function describeDeleteScope(scope) {
    const parts = [];
    if (scope.site_id) {
        const site = state.sites.find((s) => Number(s.id) === scope.site_id);
        parts.push(`وب‌سایت «${site ? site.title : scope.site_id}»`);
    } else {
        parts.push("همه وب‌سایت‌ها");
    }
    if (scope.date_from || scope.date_to) {
        parts.push(`بازه تاریخ ${scope.date_from || "ابتدا"} تا ${scope.date_to || "امروز"}`);
    }
    if (scope.media_type) {
        parts.push(`نوع محتوا: ${MEDIA_LABELS[scope.media_type] || scope.media_type}`);
    }
    if (scope.search) {
        parts.push(`جستجو: «${scope.search}»`);
    }
    return parts.join(" • ");
}

async function loadCleanupTab() {
    try {
        const data = await apiFetch("/api/overview");
        const stats = data.stats || {};
        const postsEl = $("#cleanupTotalPosts");
        const sitesEl = $("#cleanupTotalSites");
        if (postsEl) postsEl.textContent = formatNumberFa(stats.total_posts);
        if (sitesEl) sitesEl.textContent = formatNumberFa(stats.total_sites);
        syncSelectionUI();
    } catch (err) {
        showToast(err.message, "error");
    }
}

async function previewScopedDelete() {
    const box = $("#deletePreviewBox");
    const scope = collectDeleteScope();
    box.className = "preview-box";
    box.textContent = "در حال شمارش ...";

    try {
        const query = buildQuery({
            site_id: scope.site_id,
            search: scope.search,
            date_from: scope.date_from,
            date_to: scope.date_to,
            media_type: scope.media_type,
            page: 1,
            page_size: 1,
        });
        const data = await apiFetch(`/api/posts${query}`);
        const total = data.total || 0;

        if (total === 0) {
            box.className = "preview-box safe";
            box.textContent = "هیچ مطلبی مطابق این شرایط وجود ندارد؛ حذفی انجام نمی‌شود.";
            return 0;
        }

        box.className = "preview-box danger";
        box.innerHTML =
            `<strong>${formatNumberFa(total)} مطلب</strong> مطابق شرایط زیر پیدا شد: <br>` +
            `<span>${escapeHtml(describeDeleteScope(scope))}</span>`;
        return total;
    } catch (err) {
        box.className = "preview-box danger";
        box.textContent = err.message;
        return -1;
    }
}

async function runScopedDelete(button) {
    const scope = collectDeleteScope();
    const preview = await previewScopedDelete();
    if (preview === 0) {
        showToast("مطلبی برای حذف وجود ندارد.", "info");
        return;
    }

    const confirmed = window.confirm(
        `آیا از حذف ${preview > 0 ? toFa(preview) + " مطلب" : "مطالب"} مطابق شرایط زیر مطمئن هستید؟\n\n${describeDeleteScope(scope)}\n\nاین عملیات قابل بازگشت نیست.`
    );
    if (!confirmed) return;

    setBusy(button, true, "در حال حذف...");
    try {
        const result = await apiFetch("/api/posts/bulk-delete", { method: "POST", body: scope });
        showToast(result.message || "حذف انجام شد.", "success", 6000);
        const box = $("#deletePreviewBox");
        if (box) {
            box.className = "preview-box safe";
            box.textContent = result.message || "حذف انجام شد.";
        }
        await Promise.all([
            loadSites(),
            loadOverview(),
            loadPosts(),
            loadCleanupTab(),
            loadLogs(),
        ]);
    } catch (err) {
        showToast(err.message, "error", 7000);
    } finally {
        setBusy(button, false);
    }
}

function resetDeleteScope() {
    $("#deleteScopeSite").value = "";
    $("#deleteScopeMedia").value = "all";
    $("#deleteScopeDateFrom").value = "";
    $("#deleteScopeDateTo").value = "";
    $("#deleteScopeSearch").value = "";
    $("#deleteResetCursors").checked = true;
    const box = $("#deletePreviewBox");
    box.className = "preview-box";
    box.textContent = "برای دیدن تعداد مطالب مطابق شرایط، روی «پیش‌نمایش» بزنید.";
}

/* ------------------------------ Backup / Restore ------------------------------ */

function backupOptions() {
    return {
        includeSettings: Boolean(($("#backupIncludeSettings") || {}).checked),
        includeSecrets: Boolean(($("#backupIncludeSecrets") || {}).checked),
        onlyActive: Boolean(($("#backupOnlyActive") || {}).checked),
    };
}

function downloadBackup() {
    const opts = backupOptions();
    const query = buildQuery({
        include_settings: String(opts.includeSettings),
        include_secrets: String(opts.includeSecrets),
        only_active: String(opts.onlyActive),
    });
    triggerDownload(`/api/backup/export${query}`);
    showToast("فایل پشتیبان در حال دانلود است...", "success");
}

async function previewBackup() {
    const box = $("#backupPreviewBox");
    box.classList.remove("hidden");
    box.className = "preview-box";
    box.textContent = "در حال ساخت پیش‌نمایش ...";

    try {
        const data = await apiFetch("/api/backup/inspect");
        const counts = data.counts || {};
        const rows = (data.sites || [])
            .slice(0, 12)
            .map(
                (s) =>
                    `<li>${escapeHtml(s.title || s.site_input)} — ${intervalLabelFa(
                        s.interval_value,
                        s.interval_unit
                    )} — ${s.is_active ? "فعال" : "غیرفعال"}</li>`
            )
            .join("");
        const more =
            (data.sites || []).length > 12
                ? `<li>… و ${toFa((data.sites || []).length - 12)} وب‌سایت دیگر</li>`
                : "";

        box.className = "preview-box";
        box.innerHTML =
            `<strong>${formatNumberFa(counts.sites || 0)} وب‌سایت</strong> و ` +
            `<strong>${formatNumberFa(counts.settings || 0)} تنظیم</strong> در این پشتیبان قرار می‌گیرد.` +
            (rows ? `<ul class="backup-list">${rows}${more}</ul>` : "") +
            `<div class="field-hint">اطلاعات حساس: ${data.settings && data.settings.custom_cookie ? "شامل می‌شود" : "شامل نمی‌شود"}</div>`;
    } catch (err) {
        box.className = "preview-box danger";
        box.textContent = err.message;
    }
}

async function validateBackupFile() {
    const input = $("#backupFileInput");
    const box = $("#restorePreviewBox");
    if (!input || !input.files || !input.files.length) {
        showToast("ابتدا فایل پشتیبان را انتخاب کنید.", "error");
        return null;
    }

    box.classList.remove("hidden");
    box.className = "preview-box";
    box.textContent = "در حال بررسی فایل ...";

    const form = new FormData();
    form.append("file", input.files[0]);

    try {
        const res = await fetch("/api/backup/validate-file", { method: "POST", body: form });
        if (!res.ok) {
            let detail = `خطای سرور (کد ${res.status})`;
            try {
                const body = await res.json();
                if (body && body.detail) detail = body.detail;
            } catch (_) {}
            throw new Error(detail);
        }
        const data = await res.json();

        box.className = "preview-box safe";
        box.innerHTML =
            `<strong>فایل معتبر است.</strong><br>` +
            `وب‌سایت‌های داخل فایل: ${formatNumberFa(data.sites_in_file)} ` +
            `(${formatNumberFa(data.new_sites)} جدید، ${formatNumberFa(data.existing_sites)} موجود)<br>` +
            `تنظیمات: ${formatNumberFa(data.settings_in_file)} مورد` +
            (data.created_at ? `<br>تاریخ ساخت فایل: <span class="ltr-text">${toFa(data.created_at)}</span>` : "") +
            (data.secrets_included ? `<br><span class="field-hint">این فایل شامل اطلاعات حساس است.</span>` : "") +
            (data.note ? `<br>یادداشت: ${escapeHtml(data.note)}` : "");
        return data;
    } catch (err) {
        box.className = "preview-box danger";
        box.textContent = err.message;
        return null;
    }
}

async function restoreBackup(button) {
    const input = $("#backupFileInput");
    if (!input || !input.files || !input.files.length) {
        showToast("ابتدا فایل پشتیبان را انتخاب کنید.", "error");
        return;
    }

    const modeEl = document.querySelector('input[name="restoreMode"]:checked');
    const mode = modeEl ? modeEl.value : "merge";
    const restoreSettings = $("#restoreSettingsCheck").checked;
    const overwriteSettings = $("#restoreOverwriteCheck").checked;

    const modeText = mode === "replace" ? "جایگزینی کامل (وب‌سایت‌های فعلی حذف می‌شوند)" : "ادغام با فهرست فعلی";
    const confirmed = window.confirm(
        `بازیابی این فایل پشتیبان انجام شود؟\n\nروش: ${modeText}\nتنظیمات: ${restoreSettings ? "اعمال شود" : "اعمال نشود"}\n\n${mode === "replace" ? "هشدار: تمام وب‌سایت‌ها و مطالب فعلی پاک می‌شوند.\n\n" : ""}این عملیات قابل بازگشت نیست.`
    );
    if (!confirmed) return;

    setBusy(button, true, "در حال بازیابی...");
    const form = new FormData();
    form.append("file", input.files[0]);

    const query = buildQuery({
        mode,
        restore_settings: String(restoreSettings),
        overwrite_settings: String(overwriteSettings),
    });

    try {
        const res = await fetch(`/api/backup/restore-file${query}`, { method: "POST", body: form });
        if (!res.ok) {
            let detail = `خطای سرور (کد ${res.status})`;
            try {
                const body = await res.json();
                if (body && body.detail) detail = body.detail;
            } catch (_) {}
            throw new Error(detail);
        }
        const data = await res.json();

        const box = $("#restorePreviewBox");
        box.classList.remove("hidden");
        box.className = "preview-box safe";
        box.innerHTML =
            `<strong>${escapeHtml(data.message || "بازیابی انجام شد.")}</strong>` +
            (data.errors && data.errors.length
                ? `<ul class="backup-list">${data.errors
                      .slice(0, 6)
                      .map((e) => `<li>${escapeHtml(e)}</li>`)
                      .join("")}</ul>`
                : "") +
            (data.warnings && data.warnings.length
                ? `<ul class="backup-list">${data.warnings
                      .slice(0, 4)
                      .map((w) => `<li>${escapeHtml(w)}</li>`)
                      .join("")}</ul>`
                : "");

        showToast(data.message || "بازیابی انجام شد.", "success", 7000);
        await Promise.all([
            loadSites(),
            loadOverview(),
            loadPosts(),
            loadLogs(),
            loadSettings(),
            loadCleanupTab(),
        ]);
    } catch (err) {
        const box = $("#restorePreviewBox");
        box.classList.remove("hidden");
        box.className = "preview-box danger";
        box.textContent = err.message;
        showToast(err.message, "error", 7000);
    } finally {
        setBusy(button, false);
    }
}

function initBackupEvents() {
    const downloadBtn = $("#btnDownloadBackup");
    if (downloadBtn) downloadBtn.addEventListener("click", downloadBackup);

    const previewBtn = $("#btnPreviewBackup");
    if (previewBtn) previewBtn.addEventListener("click", previewBackup);

    const fileInput = $("#backupFileInput");
    if (fileInput) {
        fileInput.addEventListener("change", () => {
            const box = $("#restorePreviewBox");
            if (box) {
                box.className = "preview-box";
                box.classList.remove("hidden");
                box.textContent =
                    fileInput.files && fileInput.files.length
                        ? `فایل انتخاب شد: ${fileInput.files[0].name} — روی «بررسی فایل» بزنید.`
                        : "فایلی انتخاب نشده است.";
            }
        });
    }

    const validateBtn = $("#btnValidateBackup");
    if (validateBtn) validateBtn.addEventListener("click", validateBackupFile);

    const restoreBtn = $("#btnRestoreBackup");
    if (restoreBtn) restoreBtn.addEventListener("click", () => restoreBackup(restoreBtn));
}

function initCleanupEvents() {
    const postsBody = $("#postsTableBody");
    if (postsBody) {
        postsBody.addEventListener("change", (event) => {
            const box = event.target.closest("input[data-post-select]");
            if (!box) return;
            togglePostSelection(box.dataset.postSelect, box.checked);
        });
    }

    const selectAll = $("#selectAllPosts");
    if (selectAll) {
        selectAll.addEventListener("change", () => selectAllOnPage(selectAll.checked));
    }

    const selectMatching = $("#btnSelectAllMatching");
    if (selectMatching) selectMatching.addEventListener("click", selectAllMatchingFilter);

    const clearBtn = $("#btnClearSelection");
    if (clearBtn) clearBtn.addEventListener("click", clearSelection);

    const clearBtn2 = $("#btnClearSelectionFromCleanup");
    if (clearBtn2) clearBtn2.addEventListener("click", clearSelection);

    const deleteSelected = $("#btnDeleteSelected");
    if (deleteSelected) deleteSelected.addEventListener("click", () => deleteSelectedPosts(deleteSelected));

    const deleteSelected2 = $("#btnDeleteSelectedFromCleanup");
    if (deleteSelected2) {
        deleteSelected2.addEventListener("click", () => deleteSelectedPosts(deleteSelected2));
    }

    const goArchive = $("#btnGoToArchive");
    if (goArchive) {
        goArchive.addEventListener("click", () => {
            const tabBtn = document.querySelector('[data-tab="posts"]');
            if (tabBtn) tabBtn.click();
        });
    }

    const form = $("#bulkDeleteForm");
    if (form) {
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            runScopedDelete($("#btnApplyScopedDelete"));
        });
    }

    const previewBtn = $("#btnPreviewDelete");
    if (previewBtn) previewBtn.addEventListener("click", () => previewScopedDelete());

    const resetBtn = $("#btnResetDeleteScope");
    if (resetBtn) resetBtn.addEventListener("click", resetDeleteScope);

    const purgePosts = $("#btnPurgePostsOnly");
    if (purgePosts) {
        purgePosts.addEventListener("click", async () => {
            const confirmed = window.confirm(
                "همه مطالب و گزارش‌های دریافت پاک شوند؟\n\nوب‌سایت‌ها با تنظیماتشان باقی می‌مانند و دریافت بعدی از ابتدا انجام می‌شود.\n\nاین عملیات قابل بازگشت نیست."
            );
            if (!confirmed) return;
            setBusy(purgePosts, true, "در حال پاک‌سازی...");
            try {
                const res = await apiFetch("/api/posts/purge", {
                    method: "POST",
                    body: { keep_sites: true },
                });
                showToast(res.message, "success", 7000);
                state.selectedPostIds.clear();
                await Promise.all([loadSites(), loadOverview(), loadPosts(), loadCleanupTab(), loadLogs()]);
            } catch (err) {
                showToast(err.message, "error", 7000);
            } finally {
                setBusy(purgePosts, false);
            }
        });
    }

    const purgeAll = $("#btnPurgeEverything");
    if (purgeAll) {
        purgeAll.addEventListener("click", async () => {
            const typed = window.prompt(
                "حذف کامل همه داده‌ها (وب‌سایت‌ها + مطالب + گزارش‌ها)\n\nبرای تأیید، کلمه «حذف» را تایپ کنید:"
            );
            if (typed === null) return;
            if (typed.trim() !== "حذف") {
                showToast("عبارت تأیید درست نبود؛ عملیات لغو شد.", "error");
                return;
            }
            setBusy(purgeAll, true, "در حال حذف همه داده‌ها...");
            try {
                const res = await apiFetch("/api/posts/purge", {
                    method: "POST",
                    body: { keep_sites: false },
                });
                showToast(res.message, "success", 7000);
                state.selectedPostIds.clear();
                await Promise.all([loadSites(), loadOverview(), loadPosts(), loadCleanupTab(), loadLogs()]);
            } catch (err) {
                showToast(err.message, "error", 7000);
            } finally {
                setBusy(purgeAll, false);
            }
        });
    }

    const purgeLogs = $("#btnPurgeLogs");
    if (purgeLogs) {
        purgeLogs.addEventListener("click", async () => {
            if (!window.confirm("همه گزارش‌های دریافت پاک شوند؟")) return;
            setBusy(purgeLogs, true, "در حال پاک کردن...");
            try {
                const res = await apiFetch("/api/logs", { method: "DELETE" });
                showToast(`${toFa(res.cleared || 0)} گزارش پاک شد.`, "success");
                await Promise.all([loadCleanupTab(), loadLogs()]);
            } catch (err) {
                showToast(err.message, "error");
            } finally {
                setBusy(purgeLogs, false);
            }
        });
    }

    const deleteSingle = $("#btnDeleteSingleSite");
    if (deleteSingle) {
        deleteSingle.addEventListener("click", async () => {
            const value = $("#deleteSingleSite").value;
            if (!value) {
                showToast("ابتدا یک وب‌سایت را انتخاب کنید.", "error");
                return;
            }
            const site = state.sites.find((s) => String(s.id) === String(value));
            const confirmed = window.confirm(
                `وب‌سایت «${site ? site.title : value}» با تمام مطالب و گزارش‌هایش حذف شود؟\n\nاین عملیات قابل بازگشت نیست.`
            );
            if (!confirmed) return;
            setBusy(deleteSingle, true, "در حال حذف...");
            try {
                await apiFetch(`/api/sites/${value}`, { method: "DELETE" });
                showToast("وب‌سایت و تمام مطالب آن حذف شد.", "success");
                state.selectedPostIds.clear();
                await Promise.all([loadSites(), loadOverview(), loadPosts(), loadCleanupTab(), loadLogs()]);
            } catch (err) {
                showToast(err.message, "error");
            } finally {
                setBusy(deleteSingle, false);
            }
        });
    }
}

/* ------------------------------ Boot ------------------------------ */

function initMiscEvents() {
    const refreshSites = $("#btnRefreshSites");
    if (refreshSites) {
        refreshSites.addEventListener("click", async () => {
            await Promise.all([loadSites(), loadOverview()]);
            showToast("لیست وب‌سایت‌ها بروزرسانی شد.", "success", 2000);
        });
    }

    const syncAll = $("#btnSyncAll");
    if (syncAll) syncAll.addEventListener("click", syncAllSites);
}

async function boot() {
    initTabs();
    initModals();
    initMiscEvents();
    initSiteForm();
    initSiteTableEvents();
    initEditForm();
    initPostsEvents();
    initExports();
    initLogsEvents();
    initSettingsEvents();
    initConnectionProfileEvents();
    initCleanupEvents();
    initBackupEvents();

    resetConnectionProfileForm();

    await loadOverview();
    await loadSites();
    await loadPosts();
    await loadConnectionProfiles();

    state.overviewTimer = window.setInterval(() => {
        if (!document.hidden) loadOverview();
    }, 20000);
}

document.addEventListener("DOMContentLoaded", boot);
