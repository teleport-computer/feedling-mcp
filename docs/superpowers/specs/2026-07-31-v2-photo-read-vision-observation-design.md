# Runtime V2 `photo_read` Vision Observation Design

## Goal

Fix Hosted Runtime V2 so a model that chooses to call
`photo_read(include_image=true)` receives a real visual observation derived from
the decrypted photo instead of only `has_image` and MIME metadata.

This change is backend-only. It does not change the iOS permission UI, does not
auto-open every newly uploaded photo, and does not change Hosted V1/VPS
`io_cli photo-read --include-image` file materialization.

## Product Behavior

The existing pull-on-demand interaction remains authoritative:

1. A `photo_added` wake gives the main model the new photo ID and bounded rough
   metadata.
2. The main model decides whether the photo is worth inspecting.
3. Only a call to `photo_read` with `include_image=true` decrypts pixels and
   incurs a visual-model request.
4. The main model receives the resulting observation as untrusted data and
   decides whether to speak or remain silent.

Calling `photo_read` without `include_image=true` remains metadata-only and
must not invoke a visual provider.

## Architecture

### Capability boundary

`capabilities.photo.read` continues to resolve the stored photo to its backing
frame and decrypt it through `screen_read_core.frame_decrypt`. On success it
adds `image_b64` and `image_media_type` to the trusted in-process capability
data. The byte payload is never rendered as a textual tool result: the V2
executor removes `image_b64` before JSON serialization, matching the existing
blob-stripping invariant.

### Observation boundary

The V2 executor recognizes a successful `photo_read(include_image=true)` result
with an image payload and calls an injected photo-observer dependency before it
constructs the provider-visible `ToolResult`.

The observer follows the current visual routing policy:

1. If the user selected a dedicated vision route, use that route.
2. Otherwise use the current main provider configuration as the observer.

Both branches reuse `hosted.vision_observer.observe_image`; provider-specific
image encoding, retry behavior, and stable error classification stay in one
place. Pixels are sent only to the selected visual provider. The main reasoning
round receives a bounded `visual_observation` string, never base64.

The observation is labeled as untrusted visual data. Text visible inside a
photo is evidence to interpret, not instructions to execute.

### Dependency direction

`model_api_runtime.v2` remains independent of hosted assembly code. The worker
or executor accepts a narrow async observer callback. `serve_worker` owns the
production implementation because it can resolve saved model routes, decrypt
provider credentials, and access the active main provider configuration.

V1/VPS paths remain untouched: `tools/io_cli.py` continues to write decrypted
pixels into `IMAGE_TEMP_DIR` and returns `image_file` for the CLI model's Read
tool.

## Result and Error Semantics

A successful provider-visible tool result contains the existing photo metadata
plus:

```json
{
  "has_image": true,
  "image_media_type": "image/jpeg",
  "visual_observation": "..."
}
```

It never contains `image_b64`.

If photo lookup or decryption fails, preserve the capability's existing stable
failure behavior. If the selected visual provider fails, return a stable tool
error derived from `VisionObserverError`, such as
`vision_model_required`, `vision_model_auth_invalid`,
`vision_model_rate_limited`, or `vision_model_unavailable`. Raw provider errors,
credentials, URLs, and decrypted bytes must not enter the tool transcript.

A failed observation does not fall through from a selected dedicated route to
the main model. This preserves the user's explicit provider choice and the
existing visual trust boundary.

## Limits

- Reuse the existing V2 per-tool result character cap for the observation.
- Reuse the existing image-size/decryption limits; do not introduce an
  unbounded payload path.
- Do not persist the observation as a chat message or photo metadata.
- Do not add automatic visual calls to `photo_added` wake construction.
- Do not alter public API or OpenAPI contracts; this is an internal Hosted
  Runtime V2 capability behavior fix.

## Tests

Add regression coverage proving:

1. `photo_read(include_image=false)` returns metadata and never calls the
   observer.
2. `photo_read(include_image=true)` carries decrypted pixels to the injected
   observer and returns a provider-visible observation without base64.
3. A selected dedicated vision route is preferred over the main route.
4. Without a dedicated route, the active main provider configuration is used.
5. Dedicated-route failure does not fall through to the main route and exposes
   only a stable error code.
6. Missing frame IDs, decrypt failures, empty image bodies, and oversized or
   truncated observations remain bounded and fail safely.
7. Existing V1 `io_cli` image materialization tests continue to pass unchanged.
8. `photo_added` wake tests continue to prove that no image is automatically
   opened before the model calls `photo_read`.

## Documentation Impact

This fixes an internal implementation gap without changing the public API,
deployment topology, trust model, or user-visible product contract. No public
documentation or OpenAPI regeneration is required. The internal changelog
should record the V2 parity fix.
