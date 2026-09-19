# Priv-View Web

The phase-one Next.js application for Priv-View administrators and users. It uses the App Router, strict TypeScript, shadcn-style UI primitives, Tailwind CSS, TanStack Query, React Hook Form, Zod, Orval, and Lucide.

## Start locally

Copy the local environment file and run the web application after the FastAPI service is available on port 8000:

```bash
cp .env.example .env.local
pnpm install
pnpm dev
```

Open `http://127.0.0.1:3000`. The browser communicates only with the Next.js BFF. JWT access and refresh tokens remain in protected cookies and are never exposed to browser JavaScript.

`APP_ORIGIN` must match the browser-facing origin. Keep the example value for local development; when using `cloudflared` or another reverse proxy, set it to the public HTTPS origin and restart Next.js. Exposed environments should run `pnpm build` followed by `pnpm start`, not `pnpm dev`.

## Refresh the generated API client

From the repository root:

```bash
pipenv run python -m scripts.export_openapi
```

Then from this directory:

```bash
pnpm generate:api
```

Do not edit files under `src/generated` manually.

## Checks

```bash
pnpm lint
pnpm typecheck
pnpm test
pnpm build
```

Playwright is configured for focused end-to-end flows with `pnpm test:e2e`.
