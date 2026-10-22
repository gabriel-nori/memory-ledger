import "./Icon.css";

/**
 * The app's symbols: one set, drawn on one 24pt grid with one stroke weight,
 * so they read as a family the way SF Symbols do. They take the colour and the
 * size of the text they sit in (`currentColor`, `1em`), so they scale with the
 * user's text size and follow hover, active and disabled states for free.
 *
 * Decorative by default: the words next to an icon carry the meaning. Pass
 * `label` when the icon stands alone.
 */
const SYMBOLS = {
  menu: "M4 7h16M4 12h16M4 17h16",
  close: "M6.5 6.5l11 11M17.5 6.5l-11 11",
  check: "M5 12.5l4.5 4.5L19 7",
  "chevron-right": "M9.5 6l6 6-6 6",
  "chevron-down": "M6 9.5l6 6 6-6",
  "chevron-up": "M6 14.5l6-6 6 6",
  "arrow-right": "M5 12h14M13 6l6 6-6 6",
  "arrow-left": "M19 12H5M11 6l-6 6 6 6",
  external: "M8 16L18 6M10 6h8v8",
  search: "M16.5 16.5L21 21M11 18a7 7 0 100-14 7 7 0 000 14z",
  pencil: "M4 20l1-4.5L16 4.5a2.1 2.1 0 013 3L8 18.5 4 20z",
  map: "M9 4L3 6.5v13.5l6-2.5 6 2.5 6-2.5V4l-6 2.5L9 4zM9 4v13.5M15 6.5V20",
  dashboard: "M5 4h5a1 1 0 011 1v5a1 1 0 01-1 1H5a1 1 0 01-1-1V5a1 1 0 011-1zM14 4h5a1 1 0 011 1v5a1 1 0 01-1 1h-5a1 1 0 01-1-1V5a1 1 0 011-1zM5 13h5a1 1 0 011 1v5a1 1 0 01-1 1H5a1 1 0 01-1-1v-5a1 1 0 011-1zM14 13h5a1 1 0 011 1v5a1 1 0 01-1 1h-5a1 1 0 01-1-1v-5a1 1 0 011-1z",
  tasks: "M10.5 6.5H20M10.5 12H20M10.5 17.5H20M4 6.5l1.5 1.5L8 5M4 12l1.5 1.5L8 10.5M4 17.5L5.5 19 8 16",
  people: "M9 11a3.5 3.5 0 100-7 3.5 3.5 0 000 7zM2.5 20c0-3.3 2.9-5.5 6.5-5.5s6.5 2.2 6.5 5.5M16 4.3a3.5 3.5 0 010 6.4M18 14.8c2 .6 3.5 2.3 3.5 5.2",
  message: "M6 5h12a2.5 2.5 0 012.5 2.5v7A2.5 2.5 0 0118 17h-6l-4.5 3.5V17H6a2.5 2.5 0 01-2.5-2.5v-7A2.5 2.5 0 016 5z",
  sparkle: "M11 3.5l1.9 5.6 5.6 1.9-5.6 1.9L11 18.5l-1.9-5.6L3.5 11l5.6-1.9L11 3.5zM18.5 15l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8.8-2.2z",
  chart: "M4 4v16h16M8 15l3.5-4.5 3 3L20 7",
  cube: "M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3zM12 12l8-4.5M12 12v9M12 12L4 7.5",
  sliders: "M4 7h8M16 7h4M4 17h4M12 17h8M14 4.5v5M10 14.5v5",
  brain: "M6.5 9a2.5 2.5 0 100-5 2.5 2.5 0 000 5zM17.5 9a2.5 2.5 0 100-5 2.5 2.5 0 000 5zM12 20a2.5 2.5 0 100-5 2.5 2.5 0 000 5zM9 6.5h6M7.7 8.8l3 6.3M16.3 8.8l-3 6.3",
  book: "M4 5.5A1.5 1.5 0 015.5 4H12v16H5.5A1.5 1.5 0 014 18.5v-13zM12 4h6.5A1.5 1.5 0 0120 5.5v13a1.5 1.5 0 01-1.5 1.5H12",
  thinking: "M6 5h12a2.5 2.5 0 012.5 2.5v6A2.5 2.5 0 0118 16h-5.5L8 19.5V16H6a2.5 2.5 0 01-2.5-2.5v-6A2.5 2.5 0 016 5zM8.5 10.5h.01M12 10.5h.01M15.5 10.5h.01",
  tool: "M14.5 4.5a4.5 4.5 0 00-4.2 6.1L4.4 16.5a2 2 0 002.8 2.8l5.9-5.9a4.5 4.5 0 006.1-4.2l-2.8 2.2-2.6-.6-.6-2.6 2.2-2.8-.9-.1z",
  plug: "M9 3.5V8M15 3.5V8M6 8h12v3a6 6 0 01-12 0V8zM12 17v3.5",
  paperclip: "M19.5 11l-8 8a4.8 4.8 0 01-6.8-6.8l8.3-8.3a3.2 3.2 0 014.5 4.5l-8.3 8.3a1.6 1.6 0 01-2.3-2.3l7.6-7.6",
  mic: "M12 3.5a3 3 0 00-3 3v5.5a3 3 0 006 0V6.5a3 3 0 00-3-3zM5.5 11.5a6.5 6.5 0 0013 0M12 18v3",
  stop: "M7.5 6h9A1.5 1.5 0 0118 7.5v9a1.5 1.5 0 01-1.5 1.5h-9A1.5 1.5 0 016 16.5v-9A1.5 1.5 0 017.5 6z",
  user: "M12 12a4 4 0 100-8 4 4 0 000 8zM4.5 20.5c0-3.6 3.4-6 7.5-6s7.5 2.4 7.5 6",
  bot: "M8 8h8a3.5 3.5 0 013.5 3.5v3A3.5 3.5 0 0116 18H8a3.5 3.5 0 01-3.5-3.5v-3A3.5 3.5 0 018 8zM12 8V5M9.5 13h.01M14.5 13h.01M2.5 12.5v2M21.5 12.5v2",
  lock: "M6.5 11h11A1.5 1.5 0 0119 12.5v6a1.5 1.5 0 01-1.5 1.5h-11A1.5 1.5 0 015 18.5v-6A1.5 1.5 0 016.5 11zM8 11V8a4 4 0 018 0v3",
  link: "M10 14a4 4 0 005.7 0l3-3A4 4 0 0013 5.3l-1 1M14 10a4 4 0 00-5.7 0l-3 3A4 4 0 0011 18.7l1-1",
  blocks: "M4.5 12H15M11.5 7.5L16 12l-4.5 4.5M20 5.5v13",
  flag: "M6 21V4M6 5h11.5l-2.3 4 2.3 4H6",
  clock: "M12 20.5a8.5 8.5 0 100-17 8.5 8.5 0 000 17zM12 7.5V12l3 2",
  calendar: "M6.5 5h11A2.5 2.5 0 0120 7.5v10a2.5 2.5 0 01-2.5 2.5h-11A2.5 2.5 0 014 17.5v-10A2.5 2.5 0 016.5 5zM4 10h16M8.5 3v4M15.5 3v4",
  forward: "M15 5l5 5-5 5M20 10H10a6 6 0 00-6 6v3",
  file: "M7 3h7l5 5v11a2 2 0 01-2 2H7a2 2 0 01-2-2V5a2 2 0 012-2zM14 3v5h5",
  info: "M12 20.5a8.5 8.5 0 100-17 8.5 8.5 0 000 17zM12 11v5M12 8h.01",
  help: "M12 20.5a8.5 8.5 0 100-17 8.5 8.5 0 000 17zM9.7 9.6a2.4 2.4 0 114 1.7c-.9.7-1.7 1.1-1.7 2.2M12 16.5h.01",
  dot: "M12 12h.01",
} as const;

/** The symbols drawn solid rather than outlined, as SF does for stop and record. */
const SOLID = new Set<IconName>(["stop"]);

export type IconName = keyof typeof SYMBOLS;

export interface IconProps {
  name: IconName;
  /** Only when the icon is the whole control; otherwise the words beside it speak. */
  label?: string;
  className?: string;
}

export function Icon({ name, label, className = "" }: IconProps) {
  return (
    <svg
      className={`ml-icon${className ? ` ${className}` : ""}`}
      viewBox="0 0 24 24"
      fill={SOLID.has(name) ? "currentColor" : "none"}
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      focusable="false"
    >
      <path d={SYMBOLS[name]} />
    </svg>
  );
}
