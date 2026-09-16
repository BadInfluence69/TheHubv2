/* ==========================================================================
   The Hub — direct messages

   Everything readable happens here, in the browser. The page arrives holding
   base64 and turns it into words locally; the compose box turns words into
   base64 before anything is posted. If this file didn't run, the server would
   have nothing to show you, which is the intended shape.
   ========================================================================== */
(function () {
    "use strict";

    const Hub = (window.Hub = window.Hub || {});
    const E2EE = Hub.E2EE;

    document.addEventListener("DOMContentLoaded", init);

    function me() {
        return (window.HubUser && window.HubUser.id) || null;
    }

    function status(message, kind) {
        document.querySelectorAll("[data-key-status]").forEach((el) => {
            el.textContent = message || "";
            el.style.color = kind === "error" ? "var(--signal)"
                : kind === "ok" ? "var(--go)" : "var(--faint)";
        });
    }

    async function init() {
        if (!E2EE || !E2EE.available) {
            const reason = E2EE && E2EE.unavailableReason
                ? E2EE.unavailableReason()
                : "Encrypted messaging isn't available in this browser.";
            status(reason, "error");

            // The compose box would otherwise look usable and quietly fail.
            document.querySelectorAll("[data-dm-form]").forEach((form) => {
                form.dataset.disabled = "1";
                const input = form.querySelector("[data-dm-input]");
                const button = form.querySelector("[data-dm-send]");
                if (input) {
                    input.disabled = true;
                    input.placeholder = "Encryption unavailable — see the note above.";
                }
                if (button) button.disabled = true;
            });

            document.querySelectorAll("[data-create-key], [data-unlock-key], [data-inline-unlock]")
                .forEach((button) => (button.disabled = true));

            document.querySelectorAll("[data-locked-notice]").forEach((el) => {
                el.hidden = false;
            });
            return;
        }

        await E2EE.resume(me());

        wireKeyCreation();
        wireUnlock();
        wireRotate();
        wirePeoplePicker();
        wireBlocking();

        await renderInbox();
        await renderThread();
    }

    /* ------------------------------------------------------ key creation */
    function wireKeyCreation() {
        const button = document.querySelector("[data-create-key]");
        if (!button) return;

        button.addEventListener("click", async () => {
            const passphrase = valueOf("#new-passphrase");
            const confirmed = valueOf("#confirm-passphrase");
            const understood = document.querySelector("#understand-loss");

            if (passphrase.length < 10) {
                return status("Use a passphrase of at least 10 characters.", "error");
            }
            if (passphrase !== confirmed) {
                return status("Those two don't match.", "error");
            }
            if (understood && !understood.checked) {
                return status("Tick the box to confirm you understand there's no reset.", "error");
            }

            button.disabled = true;
            status("Generating a key pair — this takes a moment on purpose.");

            try {
                const identity = await E2EE.createIdentity(passphrase);
                await Hub.api("/messages/api/keys", { method: "POST", body: identity });
                // Unlock immediately, so the next page already works.
                await E2EE.unlock(
                    {
                        wrapped_private_key: identity.wrapped_private_key,
                        wrap_iv: identity.wrap_iv,
                        kdf_salt: identity.kdf_salt,
                        kdf_iterations: identity.kdf_iterations,
                    },
                    passphrase,
                    me()
                );
                status("Key created and unlocked on this device.", "ok");
                Hub.toast("Encryption is set up.", "success");
                setTimeout(() => (window.location.href = "/messages/"), 700);
            } catch (err) {
                status(err.message, "error");
                button.disabled = false;
            }
        });
    }

    function wireRotate() {
        const button = document.querySelector("[data-rotate-key]");
        if (!button) return;

        button.addEventListener("click", async () => {
            const warning =
                "This makes every message you've already sent or received " +
                "permanently unreadable — including for the other person. " +
                "There is no undo. Continue?";
            if (!window.confirm(warning)) return;

            const passphrase = window.prompt("New message passphrase (10+ characters)");
            if (!passphrase) return;
            if (passphrase.length < 10) {
                return status("Too short. Use at least 10 characters.", "error");
            }

            button.disabled = true;
            status("Generating a replacement key…");
            try {
                const identity = await E2EE.createIdentity(passphrase);
                identity.confirm_rotate = true;
                await Hub.api("/messages/api/keys", { method: "POST", body: identity });
                await E2EE.lock();
                await E2EE.unlock(identity, passphrase, me());
                status("New key in place. Older messages are gone for good.", "ok");
                setTimeout(() => window.location.reload(), 900);
            } catch (err) {
                status(err.message, "error");
                button.disabled = false;
            }
        });
    }

    /* ----------------------------------------------------------- unlocking */
    function wireUnlock() {
        document.querySelectorAll("[data-unlock-key], [data-inline-unlock]").forEach((button) => {
            button.addEventListener("click", () => {
                const field = button.hasAttribute("data-inline-unlock")
                    ? button.parentElement.querySelector("[data-inline-passphrase]")
                    : document.querySelector("#unlock-passphrase");
                unlockWith(field ? field.value : "");
            });
        });

        document.querySelectorAll("[data-inline-passphrase]").forEach((field) => {
            field.addEventListener("keydown", (event) => {
                if (event.key === "Enter") {
                    event.preventDefault();
                    unlockWith(field.value);
                }
            });
        });

        const lockButton = document.querySelector("[data-lock-key]");
        if (lockButton) {
            lockButton.addEventListener("click", async () => {
                await E2EE.lock();
                status("Locked. The key is gone from this browser.", "ok");
                Hub.toast("Messages locked on this device.", "info");
                setTimeout(() => window.location.reload(), 600);
            });
        }
    }

    async function unlockWith(passphrase) {
        if (!passphrase) return status("Enter your passphrase.", "error");
        status("Deriving key…");
        try {
            const record = await Hub.api("/messages/api/keys/me");
            if (!record.exists) return status("No key is set up for this account yet.", "error");
            await E2EE.unlock(record, passphrase, me());
            status("Unlocked.", "ok");
            await renderInbox();
            await renderThread();
            document.querySelectorAll("[data-locked-notice]").forEach((el) => (el.hidden = true));
        } catch (err) {
            status(err.message, "error");
        }
    }

    function showLockedNotice() {
        document.querySelectorAll("[data-locked-notice]").forEach((el) => (el.hidden = false));
    }

    /* ------------------------------------------------------------- inbox */
    async function renderInbox() {
        const rows = document.querySelectorAll("[data-conversation-list] [data-conversation]");
        if (!rows.length) return;

        if (!E2EE.isUnlocked()) {
            if (window.HubUser && window.HubUser.hasKeys) showLockedNotice();
            return;
        }

        for (const row of rows) {
            const preview = row.querySelector("[data-preview]");
            const ciphertext = row.dataset.lastCiphertext;
            const iv = row.dataset.lastIv;
            const theirKey = row.dataset.theirKey;
            if (!preview || !ciphertext || !iv || !theirKey) continue;

            const text = await E2EE.tryDecrypt(row.dataset.conversation, theirKey, ciphertext, iv);
            preview.textContent = text
                ? text.slice(0, 90)
                : "Couldn't decrypt — this may predate your current key.";
            preview.classList.toggle("faint", !text);
        }
    }

    /* ------------------------------------------------------------ thread */
    let pollTimer = null;

    async function renderThread() {
        const container = document.querySelector("[data-messages]");
        if (!container) return;

        const conversationId = container.dataset.conversation;
        const theirKey = container.dataset.theirKey;

        showFingerprint(theirKey);

        if (!E2EE.isUnlocked()) {
            if (window.HubUser && window.HubUser.hasKeys) showLockedNotice();
            return;
        }

        for (const bubble of container.querySelectorAll("[data-message]")) {
            await decryptBubble(bubble, conversationId, theirKey);
        }

        scrollToEnd(container);
        wireCompose(conversationId, theirKey);
        wireUnsend();
        startPolling(container, conversationId, theirKey);
    }

    async function decryptBubble(bubble, conversationId, theirKey) {
        if (bubble.dataset.deleted === "1") return;
        const body = bubble.querySelector("[data-body]");
        if (!body || bubble.dataset.rendered === "1") return;

        const text = await E2EE.tryDecrypt(
            conversationId, theirKey, bubble.dataset.ciphertext, bubble.dataset.iv
        );

        if (text === null) {
            body.innerHTML = '<span class="faint">Couldn\'t decrypt this message.</span>';
        } else {
            body.textContent = text;
        }
        bubble.dataset.rendered = "1";
    }

    async function showFingerprint(theirKey) {
        const target = document.querySelector("[data-their-fingerprint]");
        if (!target || !theirKey) return;
        try {
            target.textContent = await E2EE.fingerprintOf(theirKey);
        } catch (err) {
            target.textContent = "unavailable";
        }
    }

    /* ----------------------------------------------------------- compose */
    function wireCompose(conversationId, theirKey) {
        const form = document.querySelector("[data-dm-form]");
        if (!form || form.dataset.wired === "1") return;
        form.dataset.wired = "1";

        const input = form.querySelector("[data-dm-input]");
        const button = form.querySelector("[data-dm-send]");

        if (form.dataset.disabled === "1") {
            if (input) {
                input.disabled = true;
                input.placeholder = "Can't send yet — see the note above.";
            }
            if (button) button.disabled = true;
            return;
        }

        // Enter sends, shift+enter breaks the line. Standard everywhere.
        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                form.requestSubmit();
            }
        });

        input.addEventListener("input", () => {
            input.style.height = "auto";
            input.style.height = Math.min(input.scrollHeight, 180) + "px";
        });

        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const text = input.value.trim();
            if (!text) return;

            if (!E2EE.isUnlocked()) {
                showLockedNotice();
                return Hub.toast("Unlock your key before sending.", "error");
            }

            button.disabled = true;
            try {
                const sealed = await E2EE.encrypt(conversationId, theirKey, text);
                const result = await Hub.api(
                    `/messages/api/${conversationId}/send`,
                    { method: "POST", body: sealed }
                );
                appendBubble(result.id, text, true, "just now");
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

    function appendBubble(id, text, mine, when) {
        const container = document.querySelector("[data-messages]");
        if (!container) return;

        const empty = container.querySelector("[data-thread-empty]");
        if (empty) empty.remove();

        const bubble = document.createElement("div");
        bubble.className = "dm-bubble " + (mine ? "mine" : "theirs");
        bubble.dataset.message = id;
        bubble.dataset.rendered = "1";

        const body = document.createElement("div");
        body.className = "dm-bubble-body";
        body.dataset.body = "";
        body.textContent = text;

        const meta = document.createElement("div");
        meta.className = "dm-bubble-meta";
        meta.innerHTML = `<time>${when}</time>`;
        if (mine) {
            const unsend = document.createElement("button");
            unsend.type = "button";
            unsend.className = "dm-unsend";
            unsend.dataset.unsend = id;
            unsend.textContent = "Unsend";
            meta.appendChild(unsend);
        }

        bubble.append(body, meta);
        container.appendChild(bubble);
        scrollToEnd(container);
        wireUnsend();
    }

    function wireUnsend() {
        document.querySelectorAll("[data-unsend]").forEach((button) => {
            if (button.dataset.wired === "1") return;
            button.dataset.wired = "1";
            button.addEventListener("click", async () => {
                if (!window.confirm("Unsend this message?")) return;
                try {
                    await Hub.api(`/messages/api/message/${button.dataset.unsend}/delete`,
                                  { method: "POST" });
                    const bubble = button.closest("[data-message]");
                    bubble.querySelector("[data-body]").innerHTML =
                        '<span class="faint">Unsent</span>';
                    button.remove();
                } catch (err) {
                    Hub.toast(err.message, "error");
                }
            });
        });
    }

    /* ----------------------------------------------------------- polling */
    function startPolling(container, conversationId, theirKey) {
        if (pollTimer) window.clearInterval(pollTimer);

        pollTimer = window.setInterval(async () => {
            if (document.hidden || !E2EE.isUnlocked()) return;

            const bubbles = container.querySelectorAll("[data-message]");
            const last = bubbles.length
                ? Number(bubbles[bubbles.length - 1].dataset.message)
                : 0;

            try {
                const result = await Hub.api(
                    `/messages/api/${conversationId}/messages?after=${last}`
                );
                for (const message of result.messages) {
                    if (container.querySelector(`[data-message="${message.id}"]`)) continue;
                    const text = message.is_deleted
                        ? null
                        : await E2EE.tryDecrypt(
                            conversationId, theirKey, message.ciphertext, message.iv
                          );
                    appendBubble(
                        message.id,
                        message.is_deleted ? "Unsent" : (text || "Couldn't decrypt."),
                        message.sender_id === result.me,
                        "just now"
                    );
                }
            } catch (err) {
                /* A failed poll is not worth a toast; the next one will do. */
            }
        }, 6000);
    }

    function scrollToEnd(container) {
        container.scrollTop = container.scrollHeight;
    }

    /* ----------------------------------------------------- people picker */
    function wirePeoplePicker() {
        const open = document.querySelector("[data-new-message]");
        const modal = document.querySelector("[data-people-modal]");
        if (!open || !modal) return;

        const search = modal.querySelector("[data-people-search]");
        const results = modal.querySelector("[data-people-results]");

        open.addEventListener("click", () => {
            modal.hidden = false;
            search.focus();
            loadPeople("");
        });

        modal.querySelector("[data-close-modal]").addEventListener("click", () => {
            modal.hidden = true;
        });
        modal.addEventListener("click", (event) => {
            if (event.target === modal) modal.hidden = true;
        });

        let timer = null;
        search.addEventListener("input", () => {
            window.clearTimeout(timer);
            timer = window.setTimeout(() => loadPeople(search.value), 200);
        });

        async function loadPeople(term) {
            results.innerHTML = '<p class="small faint">Looking…</p>';
            try {
                const data = await Hub.api(
                    "/messages/api/people?q=" + encodeURIComponent(term)
                );
                if (!data.people.length) {
                    results.innerHTML =
                        '<p class="small faint">Nobody here by that name.</p>';
                    return;
                }
                results.innerHTML = "";
                data.people.forEach((person) => {
                    const row = document.createElement("button");
                    row.type = "button";
                    row.className = "people-row";
                    row.innerHTML = `
                        <span class="avatar avatar-sm" style="background: hsl(${person.avatar_hue} 62% 45%)">
                            ${escapeHtml((person.display_name || person.username).charAt(0).toUpperCase())}
                        </span>
                        <span class="truncate">${escapeHtml(person.display_name || person.username)}</span>
                        ${person.has_keys
                            ? '<span class="badge badge-go">ready</span>'
                            : '<span class="badge badge-amber">no key yet</span>'}`;
                    row.addEventListener("click", () => startConversation(person.id));
                    results.appendChild(row);
                });
            } catch (err) {
                results.innerHTML = `<p class="small" style="color:var(--signal)">${escapeHtml(err.message)}</p>`;
            }
        }

        async function startConversation(userId) {
            try {
                const result = await Hub.api("/messages/api/start", {
                    method: "POST",
                    body: { user_id: userId },
                });
                window.location.href = result.url;
            } catch (err) {
                Hub.toast(err.message, "error");
            }
        }
    }

    /* --------------------------------------------------------- blocking */
    function wireBlocking() {
        document.querySelectorAll("[data-block-user]").forEach((button) => {
            button.addEventListener("click", async () => {
                const undo = button.textContent.trim().toLowerCase().includes("unblock");
                try {
                    await Hub.api("/messages/api/block", {
                        method: "POST",
                        body: { user_id: Number(button.dataset.blockUser), undo },
                    });
                    window.location.reload();
                } catch (err) {
                    Hub.toast(err.message, "error");
                }
            });
        });

        document.querySelectorAll("[data-unblock]").forEach((button) => {
            button.addEventListener("click", async () => {
                try {
                    await Hub.api("/messages/api/block", {
                        method: "POST",
                        body: { user_id: Number(button.dataset.unblock), undo: true },
                    });
                    window.location.reload();
                } catch (err) {
                    Hub.toast(err.message, "error");
                }
            });
        });
    }

    /* --------------------------------------------------------- utilities */
    function valueOf(selector) {
        const el = document.querySelector(selector);
        return el ? el.value : "";
    }

    function escapeHtml(text) {
        const div = document.createElement("div");
        div.textContent = text == null ? "" : String(text);
        return div.innerHTML;
    }
})();
