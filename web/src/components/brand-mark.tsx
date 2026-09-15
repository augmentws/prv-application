import { cn } from "@/lib/utils";

export function BrandMark({ className }: { className?: string }) {
  return (
    <div
      aria-hidden="true"
      className={cn(
        "relative grid size-10 place-items-center overflow-hidden rounded-xl bg-primary shadow-sm",
        className,
      )}
    >
      <span className="absolute inset-x-0 top-0 h-1 bg-accent" />
      <span className="font-mono text-sm font-black tracking-[-0.1em] text-white">PV</span>
    </div>
  );
}
