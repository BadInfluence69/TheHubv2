/* ==========================================================================
   The Hub — watch page

   Wraps the native <video> element rather than replacing it: the browser's
   own controls stay, and this adds quality switching, resume, progress
   reporting, autoplay-next, and keyboard shortcuts on top.
   ========================================================================== */
(function () {
    "use strict";

    const Hub = window.Hub || {};
    const $ = (sel, root) => (root || document).querySelector(sel);
    const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

    const root = $("[data-player]");
    if (!root) return;

    const video = $("video", root);
    const config = JSON.parse(root.dataset.player || "{}");
    const videoId = config.videoId;

    let saveTimer = null;
    let lastSaved = 0;

    /* -------------------------------------------------------------- resume */
    function applyResume() {
        const at = Number(config.resumeAt || 0);
        if (!video || at < 30) return;

        const seekWhenReady = () => {
            if (video.duration && at < video.duration - 15) {
                video.currentTime = at;
                showResumeNote(at);
            }
            video.removeEventListener("loadedmetadata", seekWhenReady);
        };

        if (video.readyState >= 1) seekWhenReady();
        else video.addEventListener("loadedmetadata", seekWhenReady);
    }

    function showResumeNote(seconds) {
        if (!Hub.toast) return;
        Hub.toast(`Picked up where you left off — ${formatTime(seconds)}`, "info");
    }

    function formatTime(total) {
        total = Math.floor(total || 0);
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        const s = total % 60;
        return h
            ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
            : `${m}:${String(s).padStart(2, "0")}`;
    }

    /* ---------------------------------------------------- progress saving */
    function saveProgress(force) {
        if (!video || !videoId) return;
        const position = video.currentTime || 0;
        if (!force && Math.abs(position - lastSaved) < 10) return;
        lastSaved = position;

        const payload = JSON.stringify({
            position,
            duration: video.duration || null,
            csrf_token: document.querySelector('meta[name="csrf-token"]').content,
        });

        // sendBeacon survives the page being closed mid-video.
        if (force && navigator.sendBeacon) {
            navigator.sendBeacon(
                `/api/videos/${encodeURIComponent(videoId)}/progress`,
                new Blob([payload], { type: "application/json" })
            );
            return;
        }

        fetch(`/api/videos/${encodeURIComponent(videoId)}/progress`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content,
            },
            body: payload,
            keepalive: true,
        }).catch(() => {});
    }

    /* -------------------------------------------------------- quality menu */
    function initQuality() {
        const select = $("[data-quality]");
        if (!select || !video) return;

        select.addEventListener("change", async () => {
            const url = select.value;
            if (!url) return;

            const wasPlaying = !video.paused;
            const at = video.currentTime;

            video.src = url;
            video.load();
            video.addEventListener(
                "loadedmetadata",
                () => {
                    video.currentTime = at;
                    if (wasPlaying) video.play().catch(() => {});
                },
                { once: true }
            );
        });
    }

    /* ------------------------------------------------------------- reload */
    function initReload() {
        const button = $("[data-reload-stream]");
        if (!button) return;

        button.addEventListener("click", async () => {
            button.disabled = true;
            const original = button.textContent;
            button.textContent = "Resolving…";
            try {
                const data = await Hub.api(
                    `/api/stream/${encodeURIComponent(videoId)}?refresh=1`
                );
                if (video && data.url) {
                    const at = video.currentTime;
                    video.src = data.url;
                    video.load();
                    video.addEventListener(
                        "loadedmetadata",
                        () => {
                            video.currentTime = at;
                            video.play().catch(() => {});
                        },
                        { once: true }
                    );
                    Hub.toast("Fresh stream loaded", "success");
                } else {
                    window.location.reload();
                }
            } catch (err) {
                Hub.toast(err.message, "error");
            } finally {
                button.disabled = false;
                button.textContent = original;
            }
        });
    }

    /* ----------------------------------------------------------- autoplay */
    function initAutoplay() {
        if (!video) return;

        video.addEventListener("ended", async () => {
            saveProgress(true);
            const toggle = $("[data-autoplay-toggle]");
            const on = toggle ? toggle.checked : config.autoplay;
            if (!on) return;

            try {
                const data = await Hub.api(
                    "/api/next-video?current=" + encodeURIComponent(videoId)
                );
                if (data.next_id) {
                    showNextCountdown(data);
                }
            } catch (err) {
                /* nothing to roll into */
            }
        });
    }

    function showNextCountdown(next) {
        const shell = $(".player-shell");
        if (!shell) return;

        const panel = document.createElement("div");
        panel.className = "player-fallback";
        panel.innerHTML = `
            <div>
                <div class="eyebrow">Up next in <span data-count>8</span></div>
                <p style="font-size:16px;color:var(--text);font-weight:600;margin-bottom:6px">
                    ${Hub.escapeHtml(next.title || "Next video")}
                </p>
                <p>${Hub.escapeHtml(next.channel || "")}</p>
                <div class="btn-group" style="justify-content:center">
                    <button type="button" class="btn btn-primary" data-go>Play now</button>
                    <button type="button" class="btn btn-ghost" data-cancel>Stay here</button>
                </div>
            </div>`;
        shell.appendChild(panel);

        let remaining = 8;
        const counter = $("[data-count]", panel);
        const timer = setInterval(() => {
            remaining -= 1;
            if (counter) counter.textContent = String(remaining);
            if (remaining <= 0) {
                clearInterval(timer);
                go();
            }
        }, 1000);

        function go() {
            window.location.href = "/watch/" + encodeURIComponent(next.next_id);
        }

        $("[data-go]", panel).addEventListener("click", () => {
            clearInterval(timer);
            go();
        });
        $("[data-cancel]", panel).addEventListener("click", () => {
            clearInterval(timer);
            panel.remove();
        });
    }

    /* --------------------------------------------------------- shortcuts */
    function initShortcuts() {
        if (!video) return;

        document.addEventListener("keydown", (event) => {
            const tag = (event.target.tagName || "").toLowerCase();
            if (tag === "input" || tag === "textarea" || event.target.isContentEditable) {
                return;
            }
            if (event.metaKey || event.ctrlKey || event.altKey) return;

            const key = event.key.toLowerCase();
            const handlers = {
                " ": () => (video.paused ? video.play() : video.pause()),
                k: () => (video.paused ? video.play() : video.pause()),
                arrowright: () => (video.currentTime += 5),
                arrowleft: () => (video.currentTime -= 5),
                l: () => (video.currentTime += 10),
                j: () => (video.currentTime -= 10),
                arrowup: () => (video.volume = Math.min(1, video.volume + 0.1)),
                arrowdown: () => (video.volume = Math.max(0, video.volume - 0.1)),
                m: () => (video.muted = !video.muted),
                f: () => toggleFullscreen(),
                c: () => toggleCaptions(),
                ">": () => bumpRate(0.25),
                "<": () => bumpRate(-0.25),
                home: () => (video.currentTime = 0),
                end: () => (video.currentTime = video.duration || 0),
            };

            // Number keys jump to that fraction of the video.
            if (/^[0-9]$/.test(key) && video.duration) {
                event.preventDefault();
                video.currentTime = (Number(key) / 10) * video.duration;
                return;
            }

            const handler = handlers[key];
            if (handler) {
                event.preventDefault();
                handler();
            }
        });
    }

    function bumpRate(delta) {
        video.playbackRate = Math.min(3, Math.max(0.25, video.playbackRate + delta));
        if (Hub.toast) Hub.toast(`Speed ${video.playbackRate.toFixed(2)}×`, "info");
    }

    function toggleFullscreen() {
        const shell = $(".player-shell");
        if (!document.fullscreenElement) {
            (shell || video).requestFullscreen?.().catch(() => {});
        } else {
            document.exitFullscreen?.();
        }
    }

    function toggleCaptions() {
        const tracks = video.textTracks;
        if (!tracks || !tracks.length) return;
        for (let i = 0; i < tracks.length; i += 1) {
            tracks[i].mode = tracks[i].mode === "showing" ? "disabled" : "showing";
            break;
        }
    }

    /* --------------------------------------------------------- seek links */
    function initSeekLinks() {
        document.addEventListener("click", (event) => {
            const button = event.target.closest("[data-seek]");
            if (!button || !video) return;
            event.preventDefault();
            video.currentTime = Number(button.dataset.seek) || 0;
            video.play().catch(() => {});
            video.scrollIntoView({ behavior: "smooth", block: "center" });
        });
    }

    /* -------------------------------------------------- description toggle */
    function initDescription() {
        const button = $("[data-description-toggle]");
        const body = $(".description-body");
        if (!button || !body) return;

        if (body.scrollHeight <= body.clientHeight + 4) {
            button.hidden = true;
            body.classList.add("expanded");
            return;
        }

        button.addEventListener("click", () => {
            const expanded = body.classList.toggle("expanded");
            button.textContent = expanded ? "Show less" : "Show more";
        });
    }

    /* --------------------------------------------------------------- boot */
    if (video) {
        applyResume();
        video.addEventListener("timeupdate", () => saveProgress(false));
        video.addEventListener("pause", () => saveProgress(true));
        window.addEventListener("pagehide", () => saveProgress(true));
        document.addEventListener("visibilitychange", () => {
            if (document.visibilityState === "hidden") saveProgress(true);
        });

        video.addEventListener("error", () => {
            const shell = $(".player-shell");
            if (shell && !$(".player-fallback", shell)) {
                Hub.toast(
                    "The stream stopped working — try “Reload stream”.",
                    "error"
                );
            }
        });

        const stored = Number(localStorage.getItem("hub.volume"));
        if (!isNaN(stored) && stored >= 0 && stored <= 1) video.volume = stored;
        video.addEventListener("volumechange", () => {
            localStorage.setItem("hub.volume", String(video.volume));
        });
    }

    initQuality();
    initReload();
    initAutoplay();
    initShortcuts();
    initSeekLinks();
    initDescription();
    clearInterval(saveTimer);
})();

/* ==========================================================================
   Comments
   ========================================================================== */
(function () {
    "use strict";

    const Hub = window.Hub || {};
    const $ = (sel, root) => (root || document).querySelector(sel);
    const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

    const section = $("[data-comments]");
    if (!section) return;

    const videoId = section.dataset.comments;

    /* ------------------------------------------------------ auto-grow box */
    function autoGrow(textarea) {
        textarea.style.height = "auto";
        textarea.style.height = Math.min(textarea.scrollHeight, 320) + "px";
    }

    document.addEventListener("input", (event) => {
        if (event.target.matches(".comment-input")) autoGrow(event.target);
    });

    /* ------------------------------------------------------- post a comment */
    const composer = $("[data-comment-form]", section);
    if (composer) {
        const textarea = $("textarea", composer);
        const actions = $(".compose-actions", composer);
        const attach = $("[data-response-attach]", composer);
        const fileInput = $("[data-response-file]", composer);
        const refInput = $("[data-response-ref]", composer);

        textarea.addEventListener("focus", () => {
            if (actions) actions.hidden = false;
        });

        /* --------------------------------------------- video response box */
        $("[data-response-toggle]", composer)?.addEventListener("click", () => {
            if (!attach) return;
            attach.hidden = !attach.hidden;
            if (!attach.hidden) fileInput?.focus();
        });

        $("[data-response-cancel]", composer)?.addEventListener("click", () => {
            if (!attach) return;
            attach.hidden = true;
            if (fileInput) fileInput.value = "";
            if (refInput) refInput.value = "";
        });

        function resetComposer() {
            textarea.value = "";
            autoGrow(textarea);
            if (actions) actions.hidden = true;
            if (attach) attach.hidden = true;
            if (fileInput) fileInput.value = "";
            if (refInput) refInput.value = "";
        }

        $("[data-cancel-comment]", composer)?.addEventListener("click", resetComposer);

        composer.addEventListener("submit", async (event) => {
            event.preventDefault();

            const body = textarea.value.trim();
            const hasFile = fileInput && fileInput.files.length > 0;
            const hasRef = refInput && refInput.value.trim().length > 0;

            // A video response can stand on its own — the video is the comment.
            if (!body && !hasFile && !hasRef) return;

            const submit = $("button[type=submit]", composer);
            submit.disabled = true;

            try {
                let payload;
                if (hasFile || hasRef) {
                    // Multipart, so the file can ride along. Hub.api leaves
                    // FormData alone and lets the browser set the boundary.
                    payload = new FormData(composer);
                    payload.set("body", body);
                } else {
                    payload = { body };
                }

                const data = await Hub.api(
                    `/api/videos/${encodeURIComponent(videoId)}/comments`,
                    { method: "POST", body: payload }
                );

                const list = $("[data-comment-list]", section);
                list.insertAdjacentHTML("afterbegin", data.html);
                resetComposer();
                updateCount(data.count);
                $("[data-comments-empty]")?.remove();

                // A new video response changes the shelf above, so refresh
                // rather than leave a stale count on screen.
                if ((hasFile || hasRef) && data.responses) {
                    Hub.toast("Video response posted.", "success");
                }
            } catch (err) {
                Hub.toast(err.message, "error");
            } finally {
                submit.disabled = false;
            }
        });
    }

    function updateCount(count) {
        const el = $("[data-comment-count]");
        if (el) el.textContent = Hub.compact(count);
    }

    /* ------------------------------------------------------------- replies */
    document.addEventListener("click", (event) => {
        const trigger = event.target.closest("[data-reply-toggle]");
        if (!trigger) return;
        event.preventDefault();

        const comment = trigger.closest(".comment");
        const form = $(".reply-form", comment);
        if (!form) return;

        form.classList.toggle("open");
        if (form.classList.contains("open")) $("textarea", form)?.focus();
    });

    document.addEventListener("submit", async (event) => {
        const form = event.target.closest("[data-reply-form]");
        if (!form) return;
        event.preventDefault();

        const textarea = $("textarea", form);
        const body = textarea.value.trim();
        if (!body) return;

        try {
            const data = await Hub.api(
                `/api/videos/${encodeURIComponent(videoId)}/comments`,
                { method: "POST", body: { body, parent_id: form.dataset.replyForm } }
            );
            const container = form
                .closest(".comment")
                .querySelector(".comment-replies");

            if (container) {
                container.insertAdjacentHTML("beforeend", data.html);
            } else {
                const wrap = document.createElement("div");
                wrap.className = "comment-replies";
                wrap.innerHTML = data.html;
                form.closest(".comment-main").appendChild(wrap);
            }

            textarea.value = "";
            form.classList.remove("open");
            updateCount(data.count);
        } catch (err) {
            Hub.toast(err.message, "error");
        }
    });

    /* -------------------------------------------------------- vote / edit */
    document.addEventListener("click", async (event) => {
        const button = event.target.closest("[data-comment-vote]");
        if (!button) return;
        event.preventDefault();

        const comment = button.closest(".comment");
        const id = comment.dataset.commentId;
        const value = Number(button.dataset.commentVote);

        try {
            const data = await Hub.api(`/api/comments/${id}/vote`, {
                method: "POST",
                body: { value },
            });
            $$("[data-comment-vote]", comment)
                .filter((el) => el.closest(".comment") === comment)
                .forEach((el) => {
                    el.classList.toggle(
                        "is-on",
                        Number(el.dataset.commentVote) === data.my_vote && data.my_vote !== 0
                    );
                });
            const likes = $("[data-comment-likes]", comment);
            if (likes) likes.textContent = data.likes ? Hub.compact(data.likes) : "";
        } catch (err) {
            Hub.toast(err.message, "error");
        }
    });

    document.addEventListener("click", async (event) => {
        const button = event.target.closest("[data-comment-delete]");
        if (!button) return;
        event.preventDefault();
        if (!window.confirm("Delete this comment?")) return;

        const comment = button.closest(".comment");
        try {
            await Hub.api(`/api/comments/${comment.dataset.commentId}/delete`, {
                method: "POST",
            });
            const body = $(".comment-body", comment);
            body.textContent = "Comment deleted";
            body.classList.add("deleted");
            $(".comment-actions", comment)?.remove();
        } catch (err) {
            Hub.toast(err.message, "error");
        }
    });

    document.addEventListener("click", (event) => {
        const button = event.target.closest("[data-comment-edit]");
        if (!button) return;
        event.preventDefault();

        const comment = button.closest(".comment");
        const body = $(".comment-body", comment);
        if ($("textarea", comment.querySelector(".comment-main"))) return;

        const original = body.textContent.trim();
        const editor = document.createElement("form");
        editor.innerHTML = `
            <textarea class="comment-input" rows="2"></textarea>
            <div class="compose-actions">
                <button type="button" class="btn btn-quiet btn-sm" data-cancel-edit>Cancel</button>
                <button type="submit" class="btn btn-primary btn-sm">Save</button>
            </div>`;
        const textarea = $("textarea", editor);
        textarea.value = original;

        body.hidden = true;
        body.after(editor);
        textarea.focus();
        textarea.style.height = textarea.scrollHeight + "px";

        $("[data-cancel-edit]", editor).addEventListener("click", () => {
            editor.remove();
            body.hidden = false;
        });

        editor.addEventListener("submit", async (submitEvent) => {
            submitEvent.preventDefault();
            const value = textarea.value.trim();
            if (!value) return;
            try {
                await Hub.api(`/api/comments/${comment.dataset.commentId}/edit`, {
                    method: "POST",
                    body: { body: value },
                });
                body.textContent = value;
                body.hidden = false;
                editor.remove();
                const marker = $(".comment-edited", comment);
                if (!marker) {
                    $(".comment-head", comment).insertAdjacentHTML(
                        "beforeend",
                        '<span class="comment-time comment-edited">edited</span>'
                    );
                }
            } catch (err) {
                Hub.toast(err.message, "error");
            }
        });
    });
})();
