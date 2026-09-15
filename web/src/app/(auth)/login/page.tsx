import type { Metadata } from "next";

import { BrandMark } from "@/components/brand-mark";
import { LoginForm } from "@/components/login-form";

export const metadata: Metadata = { title: "Sign in" };

export default function LoginPage() {
  return (
    <main id="main-content" className="grid min-h-screen bg-background lg:grid-cols-[minmax(22rem,0.8fr)_1.2fr]">
      <section className="relative hidden overflow-hidden bg-primary px-12 py-10 text-white lg:flex lg:flex-col lg:justify-between">
        <div className="brand-grid absolute inset-0 opacity-25" />
        <div className="relative flex items-center gap-3">
          <BrandMark className="bg-white/10 ring-1 ring-white/20" />
          <span className="text-lg font-bold tracking-tight">Priv-View</span>
        </div>

        <div className="relative max-w-lg pb-10">
          <div className="mb-6 h-1 w-14 rounded-full bg-accent" />
          <h1 className="text-balance text-4xl font-semibold leading-tight tracking-[-0.035em] xl:text-5xl">
            Intelligence built around the matter.
          </h1>
          <p className="mt-5 max-w-md text-lg leading-8 text-white/72">
            Organize clients, matters, and review metadata in one secure workspace.
          </p>
        </div>

        <p className="relative text-sm text-white/60">Secure access · Tenant isolated</p>
      </section>

      <section className="flex items-center justify-center px-6 py-12 sm:px-10">
        <div className="w-full max-w-md">
          <div className="mb-10 flex items-center gap-3 lg:hidden">
            <BrandMark />
            <span className="text-lg font-bold tracking-tight">Priv-View</span>
          </div>
          <div className="mb-8">
            <p className="mb-2 text-sm font-semibold uppercase tracking-[0.14em] text-primary">Workspace access</p>
            <h2 className="text-3xl font-semibold tracking-[-0.03em] text-foreground">Sign in</h2>
            <p className="mt-2 text-base text-muted-foreground">Use the email address and password supplied by your administrator.</p>
          </div>
          <LoginForm />
          <p className="mt-8 border-t border-border pt-6 text-sm leading-6 text-muted-foreground">
            Need access? Contact your tenant administrator.
          </p>
        </div>
      </section>
    </main>
  );
}
