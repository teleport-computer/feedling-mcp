import type { ReactNode } from 'react';

function Node({
  title,
  children,
  eyebrow,
}: {
  title: string;
  children: ReactNode;
  eyebrow?: string;
}) {
  return (
    <div className="rounded-xl border border-fd-border bg-fd-card p-4 shadow-sm">
      {eyebrow ? (
        <p className="mb-1 text-[0.68rem] font-semibold uppercase tracking-[0.16em] text-fd-muted-foreground">
          {eyebrow}
        </p>
      ) : null}
      <h4 className="m-0 text-sm font-semibold text-fd-foreground">{title}</h4>
      <p className="mt-1.5 text-xs leading-5 text-fd-muted-foreground">{children}</p>
    </div>
  );
}

function DownstreamLabel({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-col items-center py-3 text-center">
      <span aria-hidden="true" className="text-xl leading-none text-fd-primary">
        ↓
      </span>
      <span className="mt-1 text-[0.68rem] font-medium uppercase tracking-[0.12em] text-fd-muted-foreground">
        {children}
      </span>
    </div>
  );
}

function InlineFlow({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-center py-2 text-center md:px-1 md:py-0">
      <div className="md:hidden">
        <span aria-hidden="true" className="block text-lg leading-none text-fd-primary">
          ↓
        </span>
        <span className="mt-1 block text-[0.62rem] font-medium uppercase tracking-wider text-fd-muted-foreground">
          {label}
        </span>
      </div>
      <div className="hidden md:block">
        <span className="block text-[0.62rem] font-medium uppercase tracking-wider text-fd-muted-foreground">
          {label}
        </span>
        <span aria-hidden="true" className="mt-1 block text-lg leading-none text-fd-primary">
          →
        </span>
      </div>
    </div>
  );
}

export function ArchitectureDiagram() {
  return (
    <figure className="not-prose my-8">
      <div className="rounded-2xl border border-fd-border bg-fd-secondary/20 p-3 sm:p-5">
        <div className="mb-5 flex flex-wrap gap-x-4 gap-y-2 text-[0.68rem] font-medium text-fd-muted-foreground">
          <span className="inline-flex items-center gap-1.5">
            <span aria-hidden="true" className="size-2.5 rounded-sm border-2 border-fd-primary" />
            Confidential-VM boundary
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span aria-hidden="true" className="size-2.5 rounded-sm border border-fd-border bg-fd-card" />
            Persistent service
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span aria-hidden="true" className="size-2.5 rounded-sm border border-dashed border-fd-muted-foreground" />
            External recipient
          </span>
        </div>

        <section aria-labelledby="architecture-callers">
          <h3
            className="mb-2 text-[0.68rem] font-semibold uppercase tracking-[0.16em] text-fd-muted-foreground"
            id="architecture-callers"
          >
            User and integrator environment
          </h3>
          <div className="grid gap-3 sm:grid-cols-2">
            <Node eyebrow="User-controlled" title="Client apps and integrations">
              Hold the user API key and content keypair; call HTTPS and WebSocket APIs.
            </Node>
            <Node eyebrow="User-operated option" title="Independent resident consumer">
              Polls encrypted Chat work and posts encrypted replies from infrastructure the user controls.
            </Node>
          </div>
        </section>

        <DownstreamLabel>HTTPS · authentication · envelopes · attestation</DownstreamLabel>

        <section
          aria-labelledby="architecture-main-cvm"
          className="rounded-2xl border-2 border-fd-primary/60 bg-fd-primary/5 p-3 sm:p-4"
        >
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div>
              <p className="m-0 text-[0.68rem] font-semibold uppercase tracking-[0.16em] text-fd-primary">
                Managed confidential-VM boundary
              </p>
              <h3 className="m-0 mt-1 text-base font-semibold" id="architecture-main-cvm">
                Main API and trusted-decrypt plane
              </h3>
            </div>
            <span className="rounded-full border border-fd-primary/30 bg-fd-background px-2.5 py-1 text-[0.65rem] font-medium text-fd-muted-foreground">
              Separate services, one measured production deployment
            </span>
          </div>

          <div className="grid items-stretch md:grid-cols-[1fr_auto_1.2fr_auto_1.1fr]">
            <Node eyebrow="Public edge" title="TLS ingress">
              Terminates the public API certificate and forwards only configured routes.
            </Node>
            <InlineFlow label="HTTP" />
            <Node eyebrow="Application plane" title="ASGI API">
              Authenticates callers, enforces user ownership, orchestrates workflows, and persists state.
            </Node>
            <InlineFlow label="Authorized calls" />
            <Node eyebrow="Trusted decrypt" title="Attested enclave service">
              Owns the content private key and decrypts envelopes or produces limited read projections.
            </Node>
          </div>

          <p className="mb-0 mt-3 text-xs leading-5 text-fd-muted-foreground">
            The API also owns authenticated screen WebSocket ingest and wake coordination. The attestation
            endpoint lets audit-aware clients verify the measured deployment and content public key.
          </p>
        </section>

        <div className="grid gap-0 md:grid-cols-3 md:gap-3">
          <DownstreamLabel>Ciphertext and metadata</DownstreamLabel>
          <DownstreamLabel>Queue, poll, and decrypt</DownstreamLabel>
          <DownstreamLabel>Inference and delivery</DownstreamLabel>
        </div>

        <div className="grid gap-3 md:grid-cols-3">
          <section aria-labelledby="architecture-persistence" className="rounded-2xl border border-fd-border p-3">
            <h3
              className="mb-2 text-[0.68rem] font-semibold uppercase tracking-[0.16em] text-fd-muted-foreground"
              id="architecture-persistence"
            >
              Persistent services
            </h3>
            <div className="grid gap-2">
              <Node title="PostgreSQL">
                Stores accounts, workflow state, encrypted bodies, and operational metadata.
              </Node>
              <Node title="Object storage">
                Stores enabled large-object flows; encryption depends on the specific workflow.
              </Node>
            </div>
          </section>

          <section
            aria-labelledby="architecture-runner"
            className="rounded-2xl border-2 border-fd-primary/60 bg-fd-primary/5 p-3"
          >
            <p className="m-0 text-[0.68rem] font-semibold uppercase tracking-[0.16em] text-fd-primary">
              Same managed confidential-VM boundary
            </p>
            <h3 className="m-0 mb-2 mt-1 text-sm font-semibold" id="architecture-runner">
              Hosted Runtime V2 worker pool
            </h3>
            <div className="grid gap-2">
              <Node title="Pooled serve-worker">
                Runs alongside the backend inside the measured main CVM; claims durable jobs, publishes
                capacity heartbeats, and mints scoped runtime tokens per turn. During the dual-runtime
                window a separate CVM still runs the V1 resident runner.
              </Node>
              <div aria-hidden="true" className="text-center text-lg leading-none text-fd-primary">
                ↓
              </div>
              <Node title="Bounded turn slot">
                Runs the native tool loop under deadlines and a watchdog; durable state remains in PostgreSQL.
              </Node>
            </div>
          </section>

          <section
            aria-labelledby="architecture-external"
            className="rounded-2xl border border-dashed border-fd-muted-foreground/70 p-3"
          >
            <h3
              className="mb-2 text-[0.68rem] font-semibold uppercase tracking-[0.16em] text-fd-muted-foreground"
              id="architecture-external"
            >
              External recipients
            </h3>
            <div className="grid gap-2">
              <Node title="User-selected model provider">
                Receives the plaintext prompt and selected context required for inference.
              </Node>
              <Node title="Push service">
                Delivers optional notifications and Live Activity updates to registered devices.
              </Node>
            </div>
          </section>
        </div>
      </div>
      <figcaption className="mt-3 text-sm leading-6 text-fd-muted-foreground">
        Logical managed-production topology. Self-hosted deployments can co-locate components, but doing so
        changes the trust and failure boundaries.
      </figcaption>
    </figure>
  );
}
