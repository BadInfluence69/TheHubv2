/* ==========================================================================
   The Hub — public rooms

   Plain text over a polling endpoint. No encryption here on purpose: a room
   anyone with an account can walk into has no key that only the participants
   hold, and pretending otherwise would be worse than saying so.
   ========================================================================== */
(function () {
    "use strict";

    const Hub = (window.Hub = window.Hub || {});

    document.addEventListener("DOMContentLoaded", () => {
        const log = document.querySelector("[data-chat-log]");
        if (!log) return;

        scrollToEnd(log);
        wireCompose();
        wireDeletes();
        wireSettings();
        startPolling(log);
    });

    /* ----------------------------------------------------------- compose */
    function wireCompose() {
        const form = document.querySelector("[data-chat-form]");
        if (!form) return;

        const input = form.querySelector("[data-chat-input]");
        const roomId = form.dataset.room;

        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                form.requestSubmit();
            }
        });

        input.addEventListener("input", () => {
            input.style.height = "auto";
            input.style.height = Math.min(input.scrollHeight, 160) + "px";
        });

        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const body = input.value.trim();
            if (!body) return;

            const button = form.querySelector("button[type=submit]");
            button.disabled = true;

            try {
                const result = await Hub.api(`/chat/api/${roomId}/post`, {
                    method: "POST",
                    body: { body },
                });
                appendPost(result.message);
                input.value = "";
                input.style.height = "auto";
            } catch (err) {
                Hub.toast(err.message, "error");
            } finally {
                button.disabled = false;
                input.focus();
            }
        });
    }

    /* -------------------------------------------------------- rendering */
    function appendPost(post) {
        const log = document.querySelector("[data-chat-log]");
        if (!log || log.querySelector(`[data-post="${post.id}"]`)) return;

        const empty = log.querySelector("[data-chat-empty]");
        if (empty) empty.remove();

        const me = Number(log.dataset.me);
        const name = post.display_name || post.username;
        const hue = post.avatar_hue || 0;

        const wrapper = document.createElement("div");
        wrapper.className = "chat-post";
        wrapper.id = "post-" + post.id;
        wrapper.dataset.post = post.id;
        wrapper.innerHTML = `
            <span class="avatar avatar-sm" style="background: hsl(${hue} 62% 44%)">
                ${escapeHtml(name.charAt(0).toUpperCase())}
            </span>
            <div class="chat-post-main">
                <div class="chat-post-head">
                    <span class="comment-author">${escapeHtml(name)}</span>
                    <time class="comment-time">just now</time>
                    ${post.user_id === me
                        ? `<button type="button" class="chat-delete" data-delete-post="${post.id}">Delete</button>`
                        : `<a class="chat-dm" href="/messages/with/${encodeURIComponent(post.username)}" title="Message privately">→</a>`}
                </div>
                <div class="chat-post-body">${linkify(post.body)}</div>
            </div>`;

        log.appendChild(wrapper);
        scrollToEnd(log);
        wireDeletes();
    }

    /**
     * Links and @mentions, escaped first.
     *
     * Order matters: everything is escaped, then patterns are matched against
     * the escaped text. Doing it the other way round would let a post inject
     * markup through a crafted URL.
     */
    function linkify(text) {
        let safe = escapeHtml(text);
        safe = safe.replace(
            /(https?:\/\/[^\s<]+)/g,
            '<a href="$1" rel="noopener noreferrer" target="_blank" class="seek-link">$1</a>'
        );
        safe = safe.replace(
            /@([A-Za-z0-9_.-]{3,32})/g,
            '<a href="/messages/with/$1" class="chat-mention">@$1</a>'
        );
        return safe.replace(/\n/g, "<br>");
    }

    /* ---------------------------------------------------------- deleting */
    function wireDeletes() {
        document.querySelectorAll("[data-delete-post]").forEach((button) => {
            if (button.dataset.wired === "1") return;
            button.dataset.wired = "1";

            button.addEventListener("click", async () => {
                if (!window.confirm("Delete this post?")) return;
                try {
                    await Hub.api(`/chat/api/post/${button.dataset.deletePost}/delete`,
                                  { method: "POST" });
                    const post = button.closest("[data-post]");
                    post.querySelector(".chat-post-body").innerHTML =
                        '<span class="faint">Deleted</span>';
                    button.remove();
                } catch (err) {
                    Hub.toast(err.message, "error");
                }
            });
        });
    }

    /* ---------------------------------------------------------- settings */
    function wireSettings() {
        const open = document.querySelector("[data-room-settings]");
        const modal = document.querySelector("[data-room-modal]");
        if (!open || !modal) return;

        open.addEventListener("click", () => (modal.hidden = false));
        modal.querySelector("[data-close-modal]").addEventListener("click", () => {
            modal.hidden = true;
        });
        modal.addEventListener("click", (event) => {
            if (event.target === modal) modal.hidden = true;
        });
    }

    /* ----------------------------------------------------------- polling */
    function startPolling(log) {
        const roomId = log.dataset.room;

        window.setInterval(async () => {
            if (document.hidden) return;

            const posts = log.querySelectorAll("[data-post]");
            const last = posts.length ? Number(posts[posts.length - 1].dataset.post) : 0;

            try {
                const result = await Hub.api(`/chat/api/${roomId}/messages?after=${last}`);
                result.messages.forEach(appendPost);
            } catch (err) {
                /* Quiet failure; the next tick retries. */
            }
        }, 5000);
    }

    /* --------------------------------------------------------- utilities */
    function scrollToEnd(element) {
        element.scrollTop = element.scrollHeight;
    }

    function escapeHtml(text) {
        const div = document.createElement("div");
        div.textContent = text == null ? "" : String(text);
        return div.innerHTML;
    }
})();
