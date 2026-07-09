# License public keyring

This directory holds the **Ed25519 PUBLIC keys** used to verify Enterprise license
tokens **offline** (`backend/app/core/edition.py::load_keyring`).

- One file per key: `<kid>.pem` — the filename stem is the JWT `kid`.
- **Public keys only.** The corresponding **private** keys live exclusively in the
  commercial billing-plane (`centralops-commercial`) and must NEVER be committed to
  this (public, AGPL) repo nor to the EE repo. A CI secret-scan should reject any
  private key here.
- Ships **empty** → the product runs as **Community** by default (fail-closed).
- Production embeds the billing-plane's current public key here; rotation = add the
  new `<kid>.pem` and stop signing with the old `kid` (drop it in a later release).
- Override the directory for testing/custom deploys with
  `CENTRALOPS_LICENSE_KEYS_DIR`.

The license is a **feature-gate, not DRM** (the AGPL core is recompilable; the real
protection is that EE code is absent from the Community artifact).
