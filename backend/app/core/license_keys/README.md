# License public keyring

This directory holds the **Ed25519 PUBLIC keys** used to verify Enterprise license
tokens **offline** (`backend/app/core/edition.py::load_keyring`).

- One file per key: `<kid>.pem` — the filename stem is the JWT `kid`.
- **Public keys only.** The corresponding **private** signing keys live exclusively in
  the license-signing service (server-side, in a KMS/Vault) and must NEVER be committed
  to this (public, AGPL) repo. A CI secret-scan should reject any private key here.
- Ships **empty** → the product runs as **Community** by default (fail-closed).
- Production embeds the current public verification key here; rotation = add the new
  `<kid>.pem` and stop signing with the old `kid` (drop it in a later release).
- Override the directory for testing/custom deploys with
  `CENTRALOPS_LICENSE_KEYS_DIR`.

The license is a **feature-gate, not DRM** (the AGPL core is recompilable; the real
protection is that EE code is absent from the Community artifact).
