/* ==========================================================================
   The Hub — end-to-end encryption for direct messages

   The privacy page claims the server cannot read direct messages. This file
   is the reason that claim is true. If you change anything in here, change
   the privacy page to match, or stop making the claim.

   How it works
   ------------
   Each account has an ECDH P-256 key pair, generated in the browser.

     · The public half goes to the server in the clear. It is meant to be
       handed out; that is how anyone else encrypts something to you.

     · The private half is exported, encrypted with AES-GCM under a key
       derived by PBKDF2-SHA256 from a passphrase, and only then uploaded.
       The server stores that ciphertext, the nonce, and the KDF parameters.

   The passphrase is never sent anywhere. It is not the account password and
   the two are deliberately kept separate: the server necessarily sees the
   account password at sign-in, so a vault locked with that password would be
   a vault the server could open.

   A message is encrypted with a key both parties can derive and nobody else
   can: ECDH between the sender's private key and the recipient's public key,
   run through HKDF. The server sees base64 and a nonce.

   What this does NOT do, stated plainly rather than left for someone to find
   out the hard way:

     · No forward secrecy. There is no ratchet. One long-lived key pair per
       account, so a passphrase compromise exposes the whole history.
     · No protection against a hostile server swapping the public key it hands
       you for one it holds the private half of. Compare fingerprints out of
       band if that is in your threat model — the UI shows them for exactly
       this reason.
     · Nothing is hidden about who messaged whom, or when. The server routes
       the message, so the server knows.
   ========================================================================== */
(function () {
    "use strict";

    const Hub = (window.Hub = window.Hub || {});

    const KDF_ITERATIONS = 600000;
    const DB_NAME = "hub-e2ee";
    const STORE = "identity";
    const INFO = "the-hub/dm/v1";

    const subtle = window.crypto && window.crypto.subtle;
    const available = Boolean(subtle && window.indexedDB);

    /**
     * Why encryption isn't available, in terms that point at the fix.
     *
     * The usual cause is not an old browser. Web Crypto is restricted to
     * secure origins, so reaching this server at http://192.168.x.x:5002 —
     * exactly how a home server normally gets used — leaves crypto.subtle
     * undefined. "Your browser doesn't support this" would send someone off
     * to upgrade a browser that was never the problem.
     */
    function unavailableReason() {
        if (available) return null;

        const insecure =
            typeof window.isSecureContext !== "undefined" && !window.isSecureContext;

        if (insecure || (!subtle && window.location && window.location.protocol === "http:")) {
            return (
                "Encrypted messaging needs a secure connection. This page was " +
                "loaded over plain HTTP, and browsers switch off the encryption " +
                "APIs there. Reach this server over HTTPS, or via localhost, and " +
                "messaging will work."
            );
        }
        if (!subtle) {
            return "This browser doesn't provide Web Crypto, so messages can't be encrypted here.";
        }
        return "This browser has no IndexedDB, so an unlocked key can't be held between pages.";
    }

    /* --------------------------------------------------------- encodings */
    const utf8 = new TextEncoder();
    const utf8Decode = new TextDecoder();

    function toBase64(buffer) {
        const bytes = new Uint8Array(buffer);
        let binary = "";
        for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
        return btoa(binary);
    }

    function fromBase64(text) {
        const binary = atob(text);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
        return bytes;
    }

    function randomBytes(length) {
        return window.crypto.getRandomValues(new Uint8Array(length));
    }

    /* ------------------------------------------------- key storage (IDB) --
       The unwrapped private key lives in IndexedDB as a non-extractable
       CryptoKey. Structured clone keeps it a key object rather than key
       material, so it survives a page load without script ever being able to
       read the bytes back out. Locking deletes the record.                 */
    function openDb() {
        return new Promise((resolve, reject) => {
            const request = window.indexedDB.open(DB_NAME, 1);
            request.onupgradeneeded = () => {
                if (!request.result.objectStoreNames.contains(STORE)) {
                    request.result.createObjectStore(STORE);
                }
            };
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }

    async function idbPut(key, value) {
        const db = await openDb();
        return new Promise((resolve, reject) => {
            const tx = db.transaction(STORE, "readwrite");
            tx.objectStore(STORE).put(value, key);
            tx.oncomplete = () => { db.close(); resolve(); };
            tx.onerror = () => { db.close(); reject(tx.error); };
        });
    }

    async function idbGet(key) {
        const db = await openDb();
        return new Promise((resolve, reject) => {
            const tx = db.transaction(STORE, "readonly");
            const request = tx.objectStore(STORE).get(key);
            request.onsuccess = () => { db.close(); resolve(request.result || null); };
            request.onerror = () => { db.close(); reject(request.error); };
        });
    }

    async function idbDelete(key) {
        const db = await openDb();
        return new Promise((resolve, reject) => {
            const tx = db.transaction(STORE, "readwrite");
            tx.objectStore(STORE).delete(key);
            tx.oncomplete = () => { db.close(); resolve(); };
            tx.onerror = () => { db.close(); reject(tx.error); };
        });
    }

    /* -------------------------------------------------------- primitives */
    async function deriveWrappingKey(passphrase, salt, iterations) {
        const material = await subtle.importKey(
            "raw", utf8.encode(passphrase), "PBKDF2", false, ["deriveKey"]
        );
        return subtle.deriveKey(
            { name: "PBKDF2", salt, iterations, hash: "SHA-256" },
            material,
            { name: "AES-GCM", length: 256 },
            false,
            ["encrypt", "decrypt"]
        );
    }

    async function fingerprintOf(publicKeyB64) {
        const digest = await subtle.digest("SHA-256", fromBase64(publicKeyB64));
        const bytes = new Uint8Array(digest).slice(0, 10);
        return Array.from(bytes)
            .map((b) => b.toString(16).padStart(2, "0"))
            .join("")
            .toUpperCase()
            .replace(/(.{4})(?=.)/g, "$1 ");
    }

    /* --------------------------------------------------------- the API */
    const E2EE = (Hub.E2EE = {});

    E2EE.available = available;
    E2EE.unavailableReason = unavailableReason;
    E2EE.fingerprintOf = fingerprintOf;

    let unlockedKey = null;       // CryptoKey (private, non-extractable)
    let unlockedUserId = null;
    const conversationKeys = new Map();

    /**
     * Build a brand new identity. Returns the blobs the server should store —
     * this function deliberately does not upload anything itself, so the call
     * site stays honest about what leaves the browser.
     */
    E2EE.createIdentity = async function (passphrase) {
        if (!available) throw new Error("This browser has no Web Crypto support.");
        if (!passphrase || passphrase.length < 10) {
            throw new Error("Use a passphrase of at least 10 characters.");
        }

        const pair = await subtle.generateKey(
            { name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]
        );

        const publicRaw = await subtle.exportKey("spki", pair.publicKey);
        const privateRaw = await subtle.exportKey("pkcs8", pair.privateKey);

        const salt = randomBytes(16);
        const iv = randomBytes(12);
        const wrappingKey = await deriveWrappingKey(passphrase, salt, KDF_ITERATIONS);
        const wrapped = await subtle.encrypt(
            { name: "AES-GCM", iv }, wrappingKey, privateRaw
        );

        const publicKey = toBase64(publicRaw);
        return {
            public_key: publicKey,
            wrapped_private_key: toBase64(wrapped),
            wrap_iv: toBase64(iv),
            kdf_salt: toBase64(salt),
            kdf_iterations: KDF_ITERATIONS,
            fingerprint: await fingerprintOf(publicKey),
        };
    };

    /** Turn the stored blob back into a usable key, and remember it. */
    E2EE.unlock = async function (record, passphrase, userId) {
        if (!available) throw new Error("This browser has no Web Crypto support.");

        const salt = fromBase64(record.kdf_salt);
        const iv = fromBase64(record.wrap_iv);
        const iterations = record.kdf_iterations || KDF_ITERATIONS;
        const wrappingKey = await deriveWrappingKey(passphrase, salt, iterations);

        let pkcs8;
        try {
            pkcs8 = await subtle.decrypt(
                { name: "AES-GCM", iv },
                wrappingKey,
                fromBase64(record.wrapped_private_key)
            );
        } catch (err) {
            // AES-GCM authentication failed. Nothing else produces this.
            throw new Error("That passphrase doesn't open this key.");
        }

        // Re-imported without `extractable`, so from here on the bytes are
        // gone even from this script's reach.
        const privateKey = await subtle.importKey(
            "pkcs8", pkcs8, { name: "ECDH", namedCurve: "P-256" }, false, ["deriveBits"]
        );

        unlockedKey = privateKey;
        unlockedUserId = userId;
        conversationKeys.clear();

        await idbPut("private", privateKey);
        await idbPut("user", userId);
        return true;
    };

    /** Restore a key unlocked earlier in this browser. */
    E2EE.resume = async function (userId) {
        if (!available) return false;
        if (unlockedKey && unlockedUserId === userId) return true;
        try {
            const storedUser = await idbGet("user");
            if (storedUser !== userId) return false;
            const key = await idbGet("private");
            if (!key) return false;
            unlockedKey = key;
            unlockedUserId = userId;
            return true;
        } catch (err) {
            return false;
        }
    };

    E2EE.isUnlocked = function () {
        return Boolean(unlockedKey);
    };

    E2EE.lock = async function () {
        unlockedKey = null;
        unlockedUserId = null;
        conversationKeys.clear();
        try {
            await idbDelete("private");
            await idbDelete("user");
        } catch (err) {
            /* Nothing useful to do; the in-memory copy is already gone. */
        }
    };

    /* --------------------------------------------------- message crypto */

    /**
     * The AES key for a conversation: static-static ECDH, then HKDF.
     *
     * The HKDF salt is derived from both public keys sorted, so both sides
     * compute the same value without having to agree on who is "first".
     */
    async function conversationKey(conversationId, theirPublicKeyB64) {
        const cached = conversationKeys.get(conversationId);
        if (cached) return cached;
        if (!unlockedKey) throw new Error("Your messages are locked.");
        if (!theirPublicKeyB64) {
            throw new Error("That account hasn't set up encryption yet.");
        }

        const theirKey = await subtle.importKey(
            "spki",
            fromBase64(theirPublicKeyB64),
            { name: "ECDH", namedCurve: "P-256" },
            false,
            []
        );

        const shared = await subtle.deriveBits(
            { name: "ECDH", public: theirKey }, unlockedKey, 256
        );

        const hkdfInput = await subtle.importKey("raw", shared, "HKDF", false, ["deriveKey"]);
        const salt = utf8.encode(INFO + ":" + conversationId);

        const key = await subtle.deriveKey(
            { name: "HKDF", hash: "SHA-256", salt, info: utf8.encode(INFO) },
            hkdfInput,
            { name: "AES-GCM", length: 256 },
            false,
            ["encrypt", "decrypt"]
        );

        conversationKeys.set(conversationId, key);
        return key;
    }

    E2EE.encrypt = async function (conversationId, theirPublicKey, plaintext) {
        const key = await conversationKey(conversationId, theirPublicKey);
        const iv = randomBytes(12);
        const ciphertext = await subtle.encrypt(
            {
                name: "AES-GCM",
                iv,
                // Binds the ciphertext to its conversation, so a row moved to
                // another thread in the database fails to decrypt rather than
                // silently appearing somewhere it was never sent.
                additionalData: utf8.encode("dm:" + conversationId),
            },
            key,
            utf8.encode(plaintext)
        );
        return { ciphertext: toBase64(ciphertext), iv: toBase64(iv) };
    };

    E2EE.decrypt = async function (conversationId, theirPublicKey, ciphertextB64, ivB64) {
        const key = await conversationKey(conversationId, theirPublicKey);
        const plain = await subtle.decrypt(
            {
                name: "AES-GCM",
                iv: fromBase64(ivB64),
                additionalData: utf8.encode("dm:" + conversationId),
            },
            key,
            fromBase64(ciphertextB64)
        );
        return utf8Decode.decode(plain);
    };

    /** Decrypt without throwing, for lists where one bad row shouldn't stop
        the rest of the page rendering. */
    E2EE.tryDecrypt = async function (conversationId, theirPublicKey, ciphertext, iv) {
        try {
            return await E2EE.decrypt(conversationId, theirPublicKey, ciphertext, iv);
        } catch (err) {
            return null;
        }
    };
})();
