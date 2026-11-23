# Engine release keys (the SDK's trust root)

`briefcase-ai engine install` and `briefcase-ai engine verify` trust an engine release only when
its manifest is signed by a public key in this directory. Nothing else can add a key: no flag,
no environment variable, no platform response. A key is added by shipping it in an SDK release.

## Format

- One file per key, named `release-<keyid>.pub`, where `<keyid>` is the hex part of the key id
  (the first 16 hex digits are enough for the name; the SDK computes the real id from the file).
- Contents: the PEM `SubjectPublicKeyInfo` of an ECDSA P-256 key
  (`-----BEGIN PUBLIC KEY-----`). Any file matching `release-*.pub` is loaded.
- Key id: `sha256:` + hex(sha256(DER SubjectPublicKeyInfo)). The manifest's `key_id` must name
  one of the keys here. Compute it with:

  ```bash
  openssl pkey -pubin -in release-XXXX.pub -outform DER | sha256sum
  ```

The production key is an ECDSA P-256 key held by the release operator. A signature is base64 of a
DER ECDSA signature over the SHA-256 of the file (what `openssl dgst -sha256 -sign key.pem FILE | base64`
writes). Take the public key as PEM (`openssl pkey -in key.pem -pubout -out release.pub`), compute
the id, rename the file, and commit it here. The portal shows the same key and key id.

## No key yet

This build ships no key, so `briefcase-ai engine install` refuses with "no trusted release key is
bundled in this SDK build". That is intended: there is no way to install an unverified engine.

## Rotation

1. Create the new signing key. Do not sign anything with it yet.
2. Add its `release-<newid>.pub` here next to the old one and release the SDK.
3. Once users have that SDK (set `min_sdk_version` in new manifests to it), sign new releases
   with the new key. Old releases stay verifiable with the old key.
4. To retire a key (for example after a compromise), delete its file and release the SDK:
   manifests naming that `key_id` are then refused.
