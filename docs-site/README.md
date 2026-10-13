# Feedling API documentation

Static API documentation built with the official Fumadocs Next.js Static
template. The site consumes `openapi/public.json`, a filtered snapshot generated
from the FastAPI application; it does not expose FastAPI's runtime documentation
routes.

## Development

```bash
npm install
npm run dev
```

Open <http://localhost:3000/docs>.

## Refresh the OpenAPI snapshot

Install the backend Python dependencies, then run from this directory:

```bash
npm run openapi:generate
```

From the repository's local virtual environment, the equivalent command is:

```bash
../.venv/bin/python ../tools/export_public_openapi.py --output openapi/public.json
```

The exporter excludes operator-only `/admin` and `/debug` paths and adds the
documented API-key/runtime-token security schemes.

## Static build

```bash
npm run types:check
npm run lint
npm run build
```

The deployable static site is written to `out/`.

`types:check` generates the MDX source before and after Next route type generation.
The final awaited MDX CLI completes after `next typegen` exits, so its background
generation cannot leave an interrupted source file for TypeScript. Keep both
passes and the real TypeScript check; no dependency or API contract changes are
required. Run `node scripts/test-mdx-generation.mjs` for the controlled shutdown
regression (requires the locked dependencies installed and exclusive use of this
checkout's generated `.source` and `.next` directories).
