/* ==========================================================================
   The Hub — Studio

   Upload progress polling, drag-and-drop file selection, and the
   library-vs-computer source switch.
   ========================================================================== */
(function () {
    "use strict";

    const Hub = window.Hub || {};
    const $ = (sel, root) => (root || document).querySelector(sel);
    const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

    /* ================================================== progress polling == */
    const board = $("[data-upload-board]");
    if (board) {
        let interval = null;

        function statusLabel(job) {
            return {
                pending: "Queued",
                uploading: "Uploading",
                processing: "YouTube is processing",
                done: "Published",
                error: "Failed",
                cancelled: "Cancelled",
            }[job.status] || job.status;
        }

        function badgeClass(status) {
            return {
                done: "badge badge-go",
                error: "badge badge-signal",
                uploading: "badge badge-amber",
                processing: "badge badge-info",
            }[status] || "badge";
        }

        async function poll() {
            try {
                const data = await Hub.api("/studio/api/jobs");
                let anyActive = false;

                data.jobs.forEach((job) => {
                    const row = $(`[data-job="${job.id}"]`);
                    if (!row) return;

                    const bar = $("[data-job-bar]", row);
                    const label = $("[data-job-status]", row);
                    const pct = $("[data-job-percent]", row);
                    const error = $("[data-job-error]", row);

                    if (bar) {
                        bar.style.width = job.percent + "%";
                        const wrap = bar.parentElement;
                        wrap.classList.toggle("is-done", job.status === "done");
                        wrap.classList.toggle("is-error", job.status === "error");
                        wrap.classList.toggle(
                            "is-live",
                            job.status === "uploading" || job.status === "processing"
                        );
                    }
                    if (label) {
                        label.textContent = statusLabel(job);
                        label.className = badgeClass(job.status) + " job-status";
                        label.setAttribute("data-job-status", "");
                    }
                    if (pct) {
                        pct.textContent =
                            job.status === "done" ? "100%" : job.percent + "%";
                    }
                    if (error) {
                        error.textContent = job.error || "";
                        error.hidden = !job.error;
                    }

                    if (["pending", "uploading", "processing"].includes(job.status)) {
                        anyActive = true;
                    }

                    if (job.status === "done" && row.dataset.wasActive === "1") {
                        row.dataset.wasActive = "0";
                        Hub.toast(`“${job.title}” is live on YouTube`, "success");
                        setTimeout(() => window.location.reload(), 1500);
                    }
                    if (["pending", "uploading", "processing"].includes(job.status)) {
                        row.dataset.wasActive = "1";
                    }
                });

                const strip = $(".signal-strip");
                if (strip) strip.classList.toggle("is-active", anyActive);

                if (!anyActive && interval) {
                    clearInterval(interval);
                    interval = setInterval(poll, 15000);
                }
            } catch (err) {
                /* transient; the next tick will retry */
            }
        }

        poll();
        interval = setInterval(poll, 2000);
    }

    /* ================================================== upload form ====== */
    const form = $("[data-upload-form]");
    if (!form) return;

    const fileInput = $("input[type=file][name=video_file]", form);
    const dropzone = $("[data-dropzone]", form);
    const chosen = $("[data-chosen-file]", form);
    const titleInput = $("input[name=title]", form);
    const librarySelect = $("select[name=library_video]", form);
    const submit = $("button[type=submit]", form);

    /* -------------------------------------------------- source switching */
    $$("[data-source-tab]", form).forEach((tab) => {
        tab.addEventListener("click", () => {
            const target = tab.dataset.sourceTab;
            $$("[data-source-tab]", form).forEach((t) =>
                t.classList.toggle("active", t === tab)
            );
            $$("[data-source-panel]", form).forEach((panel) => {
                panel.hidden = panel.dataset.sourcePanel !== target;
            });
            // Only one source can be in play at a time.
            if (target === "library") {
                if (fileInput) fileInput.value = "";
                if (chosen) chosen.hidden = true;
            } else if (librarySelect) {
                librarySelect.value = "";
            }
            validate();
        });
    });

    /* --------------------------------------------------------- drag/drop */
    if (dropzone && fileInput) {
        ["dragenter", "dragover"].forEach((name) => {
            dropzone.addEventListener(name, (event) => {
                event.preventDefault();
                dropzone.classList.add("is-hover");
            });
        });
        ["dragleave", "drop"].forEach((name) => {
            dropzone.addEventListener(name, (event) => {
                event.preventDefault();
                dropzone.classList.remove("is-hover");
            });
        });

        dropzone.addEventListener("drop", (event) => {
            const file = event.dataTransfer?.files?.[0];
            if (!file) return;
            const transfer = new DataTransfer();
            transfer.items.add(file);
            fileInput.files = transfer.files;
            onFileChosen(file);
        });

        dropzone.addEventListener("click", () => fileInput.click());
        dropzone.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                fileInput.click();
            }
        });

        fileInput.addEventListener("change", () => {
            const file = fileInput.files?.[0];
            if (file) onFileChosen(file);
        });
    }

    function onFileChosen(file) {
        if (chosen) {
            chosen.hidden = false;
            chosen.innerHTML = `
                <strong>${Hub.escapeHtml(file.name)}</strong>
                <span class="mono faint">${formatBytes(file.size)}</span>`;
        }
        if (titleInput && !titleInput.value.trim()) {
            titleInput.value = file.name
                .replace(/\.[^.]+$/, "")
                .replace(/[._]+/g, " ")
                .replace(/\s{2,}/g, " ")
                .trim()
                .slice(0, 100);
            updateCounter(titleInput);
        }
        validate();
    }

    function formatBytes(bytes) {
        const units = ["B", "KB", "MB", "GB", "TB"];
        let value = bytes;
        let unit = 0;
        while (value >= 1024 && unit < units.length - 1) {
            value /= 1024;
            unit += 1;
        }
        return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
    }

    /* ------------------------------------------------------ char counters */
    $$("[data-counter]", form).forEach((input) => {
        input.addEventListener("input", () => updateCounter(input));
        updateCounter(input);
    });

    function updateCounter(input) {
        const target = $(`[data-counter-for="${input.name}"]`, form);
        if (!target) return;
        const max = Number(input.getAttribute("maxlength")) || 0;
        target.textContent = `${input.value.length}/${max}`;
        target.classList.toggle("over", max > 0 && input.value.length > max * 0.92);
    }

    /* ------------------------------------------------------- schedule box */
    const privacy = $("select[name=privacy]", form);
    const schedule = $("[data-schedule]", form);
    if (privacy && schedule) {
        const sync = () => {
            schedule.hidden = privacy.value === "public";
        };
        privacy.addEventListener("change", sync);
        sync();
    }

    /* ---------------------------------------------------------- validation */
    function validate() {
        if (!submit) return;
        const hasFile = fileInput?.files?.length > 0;
        const hasLibrary = librarySelect?.value;
        const hasTitle = titleInput?.value.trim().length > 0;
        submit.disabled = !(hasTitle && (hasFile || hasLibrary));
    }

    [titleInput, librarySelect].forEach((el) => {
        if (el) el.addEventListener("input", validate);
        if (el) el.addEventListener("change", validate);
    });
    validate();

    /* ---------------------------------------------- upload with progress */
    form.addEventListener("submit", (event) => {
        // Only intercept real browser uploads; library picks are instant.
        if (!fileInput?.files?.length) return;

        event.preventDefault();
        const data = new FormData(form);
        const request = new XMLHttpRequest();

        const overlay = document.createElement("div");
        overlay.className = "panel";
        overlay.style.marginTop = "18px";
        overlay.innerHTML = `
            <div class="panel-head" style="margin-bottom:10px">
                <strong>Sending the file to this server</strong>
                <span class="mono faint" data-send-percent>0%</span>
            </div>
            <div class="progress is-live"><span style="width:0"></span></div>
            <p class="field-hint">Once it lands here, the upload to YouTube
               starts automatically and you can close this page.</p>`;
        form.after(overlay);
        submit.disabled = true;
        submit.textContent = "Sending…";

        const bar = $("span", $(".progress", overlay));
        const percentLabel = $("[data-send-percent]", overlay);

        request.upload.addEventListener("progress", (progressEvent) => {
            if (!progressEvent.lengthComputable) return;
            const percent = Math.round(
                (progressEvent.loaded / progressEvent.total) * 100
            );
            bar.style.width = percent + "%";
            percentLabel.textContent = percent + "%";
        });

        request.addEventListener("load", () => {
            if (request.status >= 200 && request.status < 400) {
                window.location.href = "/studio/";
            } else {
                Hub.toast("The server refused the upload. Check the file size limit.", "error");
                submit.disabled = false;
                submit.textContent = "Publish to YouTube";
                overlay.remove();
            }
        });

        request.addEventListener("error", () => {
            Hub.toast("The connection dropped while sending the file.", "error");
            submit.disabled = false;
            submit.textContent = "Publish to YouTube";
            overlay.remove();
        });

        request.open("POST", form.action || window.location.href);
        request.setRequestHeader("X-CSRF-Token",
            document.querySelector('meta[name="csrf-token"]').content);
        request.send(data);
    });
})();
