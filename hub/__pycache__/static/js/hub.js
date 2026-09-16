/* ==========================================================================
   The Hub — shared behaviour

   Every interactive control degrades to a plain form or link if this file
   fails to load, so nothing here is load-bearing for basic use.
   ========================================================================== */
(function () {
    "use strict";

    const Hub = (window.Hub = window.Hub || {});

    /* ------------------------------------------------------------ helpers */
    const $ = (sel, root) => (root || document).querySelector(sel);
    const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

    Hub.$ = $;
    Hub.$$ = $$;

    function csrfToken() {
        const meta = $('meta[name="csrf-token"]');
        return meta ? meta.content : "";
    }

    let activeRequests = 0;

    function setBusy(busy) {
        activeRequests = Math.max(0, activeRequests + (busy ? 1 : -1));
        const strip = $(".signal-strip");
        if (strip) strip.classList.toggle("is-active", activeRequests > 0);
    }

    Hub.setBusy = setBusy;

    /** fetch() with the CSRF token attached and JSON in/out. */
    Hub.api = async function (url, options) {
        const opts = Object.assign({ method: "GET" }, options || {});
        opts.headers = Object.assign(
            {
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrfToken(),
            },
            opts.headers || {}
        );

        if (opts.body && typeof opts.body !== "string" && !(opts.body instanceof FormData)) {
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(opts.body);
        }

        setBusy(true);
        try {
            const response = await fetch(url, opts);
            const text = await response.text();
            let data = {};
            try {
                data = text ? JSON.parse(text) : {};
            } catch (err) {
                data = { error: "The server sent back something unexpected." };
            }
            if (!response.ok) {
                throw new Error(data.error || `Request failed (${response.status})`);
            }
            return data;
        } finally {
            setBusy(false);
        }
    };

    /* -------------------------------------------------------------- toast */
    Hub.toast = function (message, kind) {
        let stack = $(".flash-stack");
        if (!stack) {
            stack = document.createElement("div");
            stack.className = "flash-stack";
            document.body.appendChild(stack);
        }

        const el = document.createElement("div");
        el.className = "flash flash-" + (kind || "info");
        el.setAttribute("role", kind === "error" ? "alert" : "status");
        el.innerHTML = `<div>${escapeHtml(message)}</div>
            <button type="button" aria-label="Dismiss">&times;</button>`;
        el.querySelector("button").addEventListener("click", () => el.remove());
        stack.appendChild(el);

        setTimeout(() => {
            el.style.transition = "opacity .3s, transform .3s";
            el.style.opacity = "0";
            el.style.transform = "translateY(8px)";
            setTimeout(() => el.remove(), 320);
        }, kind === "error" ? 7000 : 4200);
    };

    function escapeHtml(value) {
        const div = document.createElement("div");
        div.textContent = value == null ? "" : String(value);
        return div.innerHTML;
    }
    Hub.escapeHtml = escapeHtml;

    Hub.compact = function (value) {
        const n = Number(value) || 0;
        if (n < 1000) return String(n);
        if (n < 1e6) return trimZero(n / 1e3) + "K";
        if (n < 1e9) return trimZero(n / 1e6) + "M";
        return trimZero(n / 1e9) + "B";
    };
    function trimZero(n) {
        return (n < 10 ? n.toFixed(1) : Math.round(n).toString()).replace(/\.0$/, "");
    }

    /* --------------------------------------------------------- side rail */
    function initRail() {
        const shell = $(".shell");
        const toggle = $("[data-rail-toggle]");
        if (!shell || !toggle) return;

        const isNarrow = () => window.matchMedia("(max-width: 900px)").matches;

        if (!isNarrow() && localStorage.getItem("hub.rail") === "collapsed") {
            shell.classList.add("rail-collapsed");
        }

        toggle.addEventListener("click", () => {
            if (isNarrow()) {
                shell.classList.toggle("rail-open");
                return;
            }
            const collapsed = shell.classList.toggle("rail-collapsed");
            localStorage.setItem("hub.rail", collapsed ? "collapsed" : "open");
        });

        document.addEventListener("click", (event) => {
            if (!isNarrow() || !shell.classList.contains("rail-open")) return;
            if (event.target.closest(".rail") || event.target.closest("[data-rail-toggle]")) return;
            shell.classList.remove("rail-open");
        });
    }

    /* ------------------------------------------------------------- theme */
    function initTheme() {
        const stored = localStorage.getItem("hub.theme");
        if (stored) document.documentElement.dataset.theme = stored;

        $$("[data-theme-toggle]").forEach((button) => {
            button.addEventListener("click", () => {
                const next =
                    document.documentElement.dataset.theme === "light" ? "dark" : "light";
                document.documentElement.dataset.theme = next;
                localStorage.setItem("hub.theme", next);
            });
        });
    }

    /* ------------------------------------------------------ search box */
    function initSearch() {
        const form = $("[data-search-form]");
        if (!form) return;

        const input = $("input[name=q]", form);
        const panel = $("[data-suggestions]");
        if (!input || !panel) return;

        let timer = null;
        let cursor = -1;
        let items = [];

        function close() {
            panel.classList.remove("open");
            panel.innerHTML = "";
            cursor = -1;
            items = [];
        }

        function render(list) {
            if (!list.length) return close();
            panel.innerHTML = list
                .map(
                    (value) => `<button type="button" role="option">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                             stroke-width="2" stroke-linecap="round">
                          <circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>
                        </svg>
                        <span>${escapeHtml(value)}</span>
                    </button>`
                )
                .join("");
            panel.classList.add("open");
            items = $$("button", panel);
            items.forEach((button, index) => {
                button.addEventListener("click", () => {
                    input.value = list[index];
                    form.submit();
                });
            });
        }

        input.addEventListener("input", () => {
            clearTimeout(timer);
            const value = input.value.trim();
            timer = setTimeout(async () => {
                try {
                    const data = await Hub.api(
                        "/api/suggest?q=" + encodeURIComponent(value)
                    );
                    render(data.suggestions || []);
                } catch (err) {
                    close();
                }
            }, 180);
        });

        input.addEventListener("keydown", (event) => {
            if (!items.length) return;
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                cursor += event.key === "ArrowDown" ? 1 : -1;
                if (cursor < 0) cursor = items.length - 1;
                if (cursor >= items.length) cursor = 0;
                items.forEach((el, i) =>
                    el.setAttribute("aria-selected", i === cursor ? "true" : "false")
                );
                input.value = items[cursor].textContent.trim();
            } else if (event.key === "Enter" && cursor >= 0) {
                event.preventDefault();
                items[cursor].click();
            } else if (event.key === "Escape") {
                close();
            }
        });

        input.addEventListener("focus", () => {
            if (!input.value) {
                Hub.api("/api/suggest?q=")
                    .then((data) => render(data.suggestions || []))
                    .catch(() => {});
            }
        });

        document.addEventListener("click", (event) => {
            if (!event.target.closest(".search")) close();
        });
    }

    /* -------------------------------------------------------------- votes */
    function initVotes() {
        document.addEventListener("click", async (event) => {
            const button = event.target.closest("[data-vote]");
            if (!button) return;
            event.preventDefault();

            const wrap = button.closest("[data-vote-group]");
            const videoId = wrap.dataset.voteGroup;
            const value = Number(button.dataset.vote);

            try {
                const data = await Hub.api(
                    `/api/videos/${encodeURIComponent(videoId)}/vote`,
                    { method: "POST", body: { value } }
                );
                $$("[data-vote]", wrap).forEach((el) => {
                    el.classList.toggle(
                        "is-on",
                        Number(el.dataset.vote) === data.my_vote && data.my_vote !== 0
                    );
                    el.setAttribute(
                        "aria-pressed",
                        Number(el.dataset.vote) === data.my_vote ? "true" : "false"
                    );
                });
                const likeCount = $("[data-like-count]", wrap);
                const dislikeCount = $("[data-dislike-count]", wrap);
                if (likeCount) likeCount.textContent = Hub.compact(data.likes);
                if (dislikeCount) dislikeCount.textContent = Hub.compact(data.dislikes);
            } catch (err) {
                Hub.toast(err.message, "error");
            }
        });
    }

    /* ------------------------------------------------------- subscriptions */

    /* ------------------------------------------------------------------
       Creator link opens.

       Counts that a link was used, and nothing else — no id, no timestamp
       leaves the page. The anchor is left alone to navigate on its own; the
       count is fire-and-forget so a slow or blocked request can never stop a
       fan reaching a creator's payment page.
       ------------------------------------------------------------------ */
    function initCreatorLinks() {
        document.addEventListener("click", (event) => {
            const link = event.target.closest("[data-creator-link]");
            if (!link) return;
            const id = link.dataset.creatorLink;
            if (!id) return;
            // Hub.api toggles a global busy indicator, which would flicker
            // the whole page for a counter nobody is waiting on. A bare
            // keepalive fetch also survives the tab being navigated away.
            try {
                fetch(`/creator/links/${id}/click`, {
                    method: "POST",
                    keepalive: true,
                    headers: {
                        "X-Requested-With": "XMLHttpRequest",
                        "X-CSRF-Token": csrfToken(),
                    },
                }).catch(() => {});
            } catch (_) {
                /* Never block the navigation. */
            }
        });
    }

    function initSubscribe() {
        document.addEventListener("click", async (event) => {
            const button = event.target.closest("[data-subscribe]");
            if (!button) return;
            event.preventDefault();

            const channel = button.dataset.subscribe;
            try {
                const data = await Hub.api("/api/subscriptions/toggle", {
                    method: "POST",
                    body: { channel },
                });
                button.classList.toggle("btn-primary", !data.subscribed);
                button.classList.toggle("btn-ghost", data.subscribed);
                const label = $("[data-subscribe-label]", button) || button;
                label.textContent = data.subscribed ? "Fan" : "Become a fan";

                $$("[data-subscriber-count]").forEach((el) => {
                    if (el.dataset.subscriberCount === channel) {
                        el.textContent = Hub.compact(data.count);
                    }
                });
                Hub.toast(
                    data.subscribed
                        ? `Following ${channel} on this server`
                        : `Unfollowed ${channel}`,
                    "success"
                );
            } catch (err) {
                Hub.toast(err.message, "error");
            }
        });
    }

    /* -------------------------------------------------------- watch later */
    function initWatchLater() {
        document.addEventListener("click", async (event) => {
            const button = event.target.closest("[data-watch-later]");
            if (!button) return;
            event.preventDefault();
            event.stopPropagation();

            try {
                const data = await Hub.api("/api/watch-later", {
                    method: "POST",
                    body: { video_id: button.dataset.watchLater },
                });
                button.classList.toggle("is-on", data.saved);
                button.title = data.saved ? "Remove from Watch later" : "Save to Watch later";
                Hub.toast(data.saved ? "Saved for later" : "Removed from Watch later", "success");
            } catch (err) {
                Hub.toast(err.message, "error");
            }
        });
    }

    /* ---------------------------------------------------- playlist picker */
    function initPlaylistModal() {
        const modal = $("[data-playlist-modal]");
        if (!modal) return;

        function open(videoId) {
            modal.dataset.videoId = videoId;
            modal.classList.add("open");
            const first = $("input[type=checkbox]", modal);
            if (first) first.focus();
        }
        function close() {
            modal.classList.remove("open");
        }

        document.addEventListener("click", (event) => {
            const trigger = event.target.closest("[data-add-to-playlist]");
            if (trigger) {
                event.preventDefault();
                open(trigger.dataset.addToPlaylist);
                return;
            }
            if (event.target.closest("[data-close-modal]") || event.target === modal) {
                close();
            }
        });

        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && modal.classList.contains("open")) close();
        });

        $$("[data-playlist-check]", modal).forEach((box) => {
            box.addEventListener("change", async () => {
                try {
                    await Hub.api("/api/playlists/add", {
                        method: "POST",
                        body: {
                            video_id: modal.dataset.videoId,
                            playlist_id: box.dataset.playlistCheck,
                        },
                    });
                } catch (err) {
                    box.checked = !box.checked;
                    Hub.toast(err.message, "error");
                }
            });
        });

        const createForm = $("[data-new-playlist]", modal);
        if (createForm) {
            createForm.addEventListener("submit", async (event) => {
                event.preventDefault();
                const input = $("input[name=title]", createForm);
                const title = input.value.trim();
                if (!title) return;
                try {
                    await Hub.api("/api/playlists/create", {
                        method: "POST",
                        body: { title, video_id: modal.dataset.videoId },
                    });
                    Hub.toast(`Created “${title}” and added the video`, "success");
                    input.value = "";
                    setTimeout(() => window.location.reload(), 700);
                } catch (err) {
                    Hub.toast(err.message, "error");
                }
            });
        }
    }

    /* --------------------------------------------------------- share menu */
    function initShare() {
        document.addEventListener("click", async (event) => {
            const button = event.target.closest("[data-share]");
            if (!button) return;
            event.preventDefault();

            const url = new URL(button.dataset.share || window.location.href, window.location.origin);
            if (navigator.share) {
                try {
                    await navigator.share({ title: document.title, url: url.href });
                    return;
                } catch (err) {
                    if (err.name === "AbortError") return;
                }
            }
            try {
                await navigator.clipboard.writeText(url.href);
                Hub.toast("Link copied", "success");
            } catch (err) {
                Hub.toast("Couldn't copy — the address bar has the link", "error");
            }
        });
    }

    /* ------------------------------------------------------- confirm forms */
    function initConfirm() {
        document.addEventListener("submit", (event) => {
            const form = event.target.closest("[data-confirm]");
            if (!form) return;
            if (!window.confirm(form.dataset.confirm)) event.preventDefault();
        });
    }

    /* ---------------------------------------------------- dismiss flashes */
    function initFlashes() {
        $$(".flash button").forEach((button) => {
            button.addEventListener("click", () => button.closest(".flash").remove());
        });
        setTimeout(() => {
            $$(".flash-success").forEach((el) => el.remove());
        }, 5000);
    }

    /* --------------------------------------------------------- shortcuts */
    function initShortcuts() {
        document.addEventListener("keydown", (event) => {
            const tag = (event.target.tagName || "").toLowerCase();
            const typing =
                tag === "input" || tag === "textarea" || event.target.isContentEditable;

            if (event.key === "/" && !typing) {
                event.preventDefault();
                const input = $("[data-search-form] input[name=q]");
                if (input) {
                    input.focus();
                    input.select();
                }
            }

            if (event.key === "Escape" && typing) event.target.blur();

            if (!typing && event.shiftKey && event.key.toLowerCase() === "?") {
                const help = $("[data-shortcuts]");
                if (help) help.classList.toggle("open");
            }
        });
    }

    /* ------------------------------------------------------ notifications */
    function initNotifications() {
        const bell = $("[data-notification-bell]");
        if (!bell) return;

        async function refresh() {
            try {
                const data = await Hub.api("/api/notifications");
                const dot = $("[data-notification-count]", bell);
                if (dot) {
                    dot.textContent = data.unread > 9 ? "9+" : String(data.unread);
                    dot.hidden = data.unread === 0;
                }
            } catch (err) {
                /* offline is fine */
            }
        }
        setInterval(refresh, 60000);
    }

    /* -------------------------------------------------- relative timestamps */
    function initTimestamps() {
        // Server-rendered times are UTC; make sure anything marked up as a
        // machine time is also readable in the visitor's own zone on hover.
        $$("time[datetime]").forEach((el) => {
            const parsed = new Date(el.getAttribute("datetime"));
            if (!isNaN(parsed)) el.title = parsed.toLocaleString();
        });
    }

    /* --------------------------------------------------------------- boot */
    document.addEventListener("DOMContentLoaded", () => {
        initRail();
        initTheme();
        initSearch();
        initVotes();
        initSubscribe();
        initCreatorLinks();
        initWatchLater();
        initPlaylistModal();
        initShare();
        initConfirm();
        initFlashes();
        initShortcuts();
        initNotifications();
        initTimestamps();
    });
})();
