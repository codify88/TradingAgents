import { twMerge } from "tailwind-merge";

/** Join class names, dropping falsy ones; later Tailwind classes win conflicts. */
export function cn(...parts: (string | false | null | undefined)[]): string {
  return twMerge(parts.filter(Boolean).join(" "));
}
