export const money = (n: number, digits = 0) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: digits });

export const time = (iso: string) =>
  new Date(iso).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false });

export const day = (iso: string) =>
  new Date(iso.length === 10 ? `${iso}T12:00:00` : iso).toLocaleDateString("en-US", {
    weekday: "short",
    day: "numeric",
    month: "short",
  });

/** "36 min left", "1 h 5 min left", "closed". */
export function remaining(seconds: number): string {
  if (seconds <= 0) return "closed";
  const m = Math.ceil(seconds / 60);
  if (m < 60) return `${m} min left`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h ${m % 60} min left`;
  return `${Math.floor(h / 24)} d ${h % 24} h left`;
}
