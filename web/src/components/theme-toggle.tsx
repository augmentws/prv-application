"use client";

import { Moon, Sun } from "lucide-react";

import { Button } from "@/components/ui/button";

export function ThemeToggle() {
  const toggleTheme = () => {
    const root = document.documentElement;
    const nextTheme = root.classList.contains("dark") ? "light" : "dark";
    root.classList.toggle("dark", nextTheme === "dark");
    root.style.colorScheme = nextTheme;
    try {
      localStorage.setItem("theme", nextTheme);
    } catch {
      // The visible theme can still change when browser storage is unavailable.
    }
  };

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      aria-label="Toggle color theme"
      onClick={toggleTheme}
    >
      <Moon className="dark:hidden" />
      <Sun className="hidden dark:block" />
    </Button>
  );
}
