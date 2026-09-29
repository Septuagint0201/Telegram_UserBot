# M8 supply-chain evidence contract

`generate_python_inventory.py` creates a deterministic, content-free CycloneDX 1.6 inventory
for installed Python distributions that match `requirements/runtime.lock`. It records declared
license metadata without copying license text, paths, environment variables, credentials, or
application data.

This inventory is deliberately scoped to Python distributions. It does **not** claim that the
production image SBOM exists or passed review. M8-001 remains `NOT BUILT / NOT GENERATED` until an
actual `linux/amd64` image is built, addressed by its immutable digest, scanned by a whole-image
OCI/OS-package scanner, and its Python and whole-image inventories receive a license review.

Example invocation inside the final runtime image:

```text
python deploy/sbom/generate_python_inventory.py \
  --lock requirements/runtime.lock \
  --image-reference registry.invalid/telegram-userbot@sha256:<64 lowercase hex> \
  --source-commit <40 lowercase hex> \
  --created-at 2026-08-23T00:00:00Z \
  --output /evidence/python-inventory.cdx.json
```

The example registry is intentionally non-routable. No image is published by this contract.
