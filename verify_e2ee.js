/* ==========================================================================
   Verification harness for e2ee.js

   The privacy page makes a specific claim: the server cannot read direct
   messages. That claim rests entirely on hub/static/js/e2ee.js, so it is
   worth actually running rather than reasoning about.

   This loads the real file — not a copy of the logic — behind a small stub
   for the browser globals it expects, and checks the properties that matter:

     · two accounts independently derive the same conversation key
     · a third account cannot derive it, even holding both public keys
     · the wrapped private key is useless without the passphrase
     · ciphertext is bound to its conversation and rejects being moved
     · nonces are unique per message

   Run:  node verify_e2ee.js
   ========================================================================== */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const GREEN = "\x1b[92m", RED = "\x1b[91m", YELLOW = "\x1b[93m", RESET = "\x1b[0m";
let passed = 0, failed = 0;

function check(label, condition, detail) {
    if (condition) {
        passed += 1;
        console.log(`  ${GREEN}pass${RESET}  ${label}`);
    } else {
        failed += 1;
        console.log(`  ${RED}FAIL${RESET}  ${label}${detail ? "  — " + detail : ""}`);
    }
}

/* ------------------------------------------------- minimal browser stubs */

/** Just enough IndexedDB to satisfy the key cache. Everything is in memory
    and thrown away when the process exits. */
function makeIndexedDb() {
    const stores = new Map();

    function fire(target, handler, value) {
        // The real API dispatches asynchronously; matching that shakes out
        // any accidental reliance on synchronous completion.
        setImmediate(() => {
            target.result = value;
            if (target[handler]) target[handler]();
        });
    }

    return {
        open(name) {
            const request = {};
            if (!stores.has(name)) stores.set(name, new Map());
            const db = {
                objectStoreNames: {
                    contains: () => true,
                },
                createObjectStore(storeName) {
                    stores.get(name).set(storeName, new Map());
                },
                close() {},
                transaction(storeName) {
                    const bucket = stores.get(name);
                    if (!bucket.has(storeName)) bucket.set(storeName, new Map());
                    const data = bucket.get(storeName);
                    const tx = {
                        objectStore() {
                            return {
                                put(value, key) {
                                    data.set(key, value);
                                    setImmediate(() => tx.oncomplete && tx.oncomplete());
                                },
                                get(key) {
                                    const req = {};
                                    fire(req, "onsuccess", data.get(key));
                                    return req;
                                },
                                delete(key) {
                                    data.delete(key);
                                    setImmediate(() => tx.oncomplete && tx.oncomplete());
                                },
                            };
                        },
                    };
                    return tx;
                },
            };
            fire(request, "onsuccess", db);
            return request;
        },
    };
}

function makeWindow() {
    return {
        crypto: globalThis.crypto,
        indexedDB: makeIndexedDb(),
    };
}

/** Load e2ee.js into its own sandbox, returning that sandbox's Hub.E2EE.
    Separate sandboxes stand in for separate browsers. */
function loadE2EE() {
    const source = fs.readFileSync(
        path.join(__dirname, "hub", "static", "js", "e2ee.js"), "utf8"
    );
    const sandbox = {
        window: makeWindow(),
        btoa: (s) => Buffer.from(s, "binary").toString("base64"),
        atob: (s) => Buffer.from(s, "base64").toString("binary"),
        TextEncoder,
        TextDecoder,
        setTimeout,
        setImmediate,
        console,
    };
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox);
    return sandbox.window.Hub.E2EE;
}

/* ---------------------------------------------------------------- tests */
(async function main() {
    console.log(`\n${YELLOW}Verifying hub/static/js/e2ee.js${RESET}`);

    console.log("\nSetup");
    const alice = loadE2EE();
    const bob = loadE2EE();
    const mallory = loadE2EE();

    check("the module loads and reports Web Crypto available", alice.available === true);

    const ALICE_PASS = "correct horse battery staple";
    const BOB_PASS = "a completely different passphrase";

    const aliceIdentity = await alice.createIdentity(ALICE_PASS);
    const bobIdentity = await bob.createIdentity(BOB_PASS);

    check("alice generates an identity", Boolean(aliceIdentity.public_key));
    check("bob generates an identity", Boolean(bobIdentity.public_key));
    check("the two public keys differ",
          aliceIdentity.public_key !== bobIdentity.public_key);
    check("the private key is uploaded wrapped, not raw",
          aliceIdentity.wrapped_private_key !== undefined &&
          aliceIdentity.wrapped_private_key.length > 0);
    check("a KDF salt is generated per identity",
          aliceIdentity.kdf_salt !== bobIdentity.kdf_salt);
    check("the iteration count is high enough to be worth something",
          aliceIdentity.kdf_iterations >= 600000,
          String(aliceIdentity.kdf_iterations));
    check("a fingerprint is produced for out-of-band checking",
          /^[0-9A-F ]{20,}$/.test(aliceIdentity.fingerprint), aliceIdentity.fingerprint);

    console.log("\nUnlocking");
    await alice.unlock(aliceIdentity, ALICE_PASS, 1);
    check("alice unlocks with the right passphrase", alice.isUnlocked());

    let refused = false;
    try {
        await bob.unlock(bobIdentity, "not the passphrase", 2);
    } catch (err) {
        refused = /passphrase/i.test(err.message);
    }
    check("the wrong passphrase is refused", refused);
    check("a failed unlock leaves the key locked", !bob.isUnlocked());

    await bob.unlock(bobIdentity, BOB_PASS, 2);
    check("bob unlocks with his own passphrase", bob.isUnlocked());

    console.log("\nMessage round trip");
    const CONVERSATION = "42";
    const PLAINTEXT = "Meet me by the bins at four. Bring the good crisps.";

    const sealed = await alice.encrypt(CONVERSATION, bobIdentity.public_key, PLAINTEXT);
    check("alice encrypts a message", Boolean(sealed.ciphertext && sealed.iv));
    check("the ciphertext does not contain the plaintext",
          !Buffer.from(sealed.ciphertext, "base64").toString("utf8").includes("crisps"));

    const opened = await bob.decrypt(
        CONVERSATION, aliceIdentity.public_key, sealed.ciphertext, sealed.iv
    );
    check("bob decrypts it back to the original", opened === PLAINTEXT, opened);

    const echoed = await alice.decrypt(
        CONVERSATION, bobIdentity.public_key, sealed.ciphertext, sealed.iv
    );
    check("alice can still read her own sent message", echoed === PLAINTEXT);

    console.log("\nWhat an eavesdropper gets");
    // Mallory has both public keys — everything the server holds — and her
    // own unlocked identity. This is exactly the server's position.
    const malloryIdentity = await mallory.createIdentity("mallory's passphrase");
    await mallory.unlock(malloryIdentity, "mallory's passphrase", 3);

    const stolen = await mallory.tryDecrypt(
        CONVERSATION, aliceIdentity.public_key, sealed.ciphertext, sealed.iv
    );
    check("a third party holding both public keys cannot decrypt", stolen === null);

    const stolenOther = await mallory.tryDecrypt(
        CONVERSATION, bobIdentity.public_key, sealed.ciphertext, sealed.iv
    );
    check("nor with the other public key", stolenOther === null);

    // The server holds the wrapped private key too. Without the passphrase
    // it is just bytes.
    const offline = loadE2EE();
    let brute = false;
    try {
        await offline.unlock(aliceIdentity, "guess", 1);
        brute = true;
    } catch (err) {
        brute = false;
    }
    check("the stored wrapped key is useless without the passphrase", !brute);

    console.log("\nBinding and nonces");
    const moved = await bob.tryDecrypt(
        "99", aliceIdentity.public_key, sealed.ciphertext, sealed.iv
    );
    check("ciphertext moved to another conversation fails to decrypt", moved === null);

    const nonces = new Set();
    const ciphertexts = new Set();
    for (let i = 0; i < 25; i += 1) {
        const each = await alice.encrypt(CONVERSATION, bobIdentity.public_key, "same text");
        nonces.add(each.iv);
        ciphertexts.add(each.ciphertext);
    }
    check("every message gets a fresh nonce", nonces.size === 25, `${nonces.size}/25`);
    check("identical plaintext produces different ciphertext each time",
          ciphertexts.size === 25, `${ciphertexts.size}/25`);

    console.log("\nLocking");
    await alice.lock();
    check("locking clears the in-memory key", !alice.isUnlocked());

    let deniedAfterLock = false;
    try {
        await alice.encrypt(CONVERSATION, bobIdentity.public_key, "should not work");
    } catch (err) {
        deniedAfterLock = /locked/i.test(err.message);
    }
    check("encrypting after locking is refused", deniedAfterLock);

    const resumed = await alice.resume(1);
    check("a locked key does not come back from storage", resumed === false);

    console.log("\nUnicode and size");
    await alice.unlock(aliceIdentity, ALICE_PASS, 1);
    const tricky = "emoji 🎬, accents éàü, CJK 日本語, and a \u0000 null";
    const sealedTricky = await alice.encrypt(CONVERSATION, bobIdentity.public_key, tricky);
    const openedTricky = await bob.decrypt(
        CONVERSATION, aliceIdentity.public_key, sealedTricky.ciphertext, sealedTricky.iv
    );
    check("unicode survives the round trip intact", openedTricky === tricky);

    const long = "x".repeat(20000);
    const sealedLong = await alice.encrypt(CONVERSATION, bobIdentity.public_key, long);
    const openedLong = await bob.decrypt(
        CONVERSATION, aliceIdentity.public_key, sealedLong.ciphertext, sealedLong.iv
    );
    check("a long message survives too", openedLong === long);
    check("a 20k message stays under the server's ciphertext cap",
          sealedLong.ciphertext.length < 64000, String(sealedLong.ciphertext.length));

    console.log();
    if (failed) {
        console.log(`${RED}${failed} check${failed === 1 ? "" : "s"} failed${RESET}, ${passed} passed.\n`);
        process.exit(1);
    }
    console.log(`${GREEN}All ${passed} checks passed.${RESET}\n`);
})().catch((err) => {
    console.error(`\n${RED}Harness crashed:${RESET}`, err);
    process.exit(1);
});
