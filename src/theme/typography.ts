// Typography scale per spec §10.2.
//
// Display: ideal is a heavy condensed grotesque (Druk Wide Bold, Founders Grotesk
// X-Condensed Bold, or Helvetica Now Display Black). Pending licensing — we stand
// in with Inter 900 + tight tracking and treat that as the brand's signature face
// behind a single token so swapping is a one-line change once licensed.
//
// UI sans: Inter (regular / medium).
// Mono caps: JetBrains Mono for eyebrows, FIG. captions, telemetry cells.
// Tabular figures are critical — telemetry must never jitter horizontally.

export const fonts = {
  display: 'Inter_900Black',          // placeholder for licensed display face
  displayItalic: 'Inter_900Black',
  bodyRegular: 'Inter_400Regular',
  bodyMedium: 'Inter_500Medium',
  bodySemibold: 'Inter_600SemiBold',
  mono: 'JetBrainsMono_400Regular',
  monoMedium: 'JetBrainsMono_500Medium',
} as const;

// Type scale (sp / pt) — matches plan §10.2.
export const typeScale = {
  displayXL: 64,
  displayLG: 48,
  displayMD: 36,
  titleLG: 28,
  titleMD: 22,
  bodyLG: 17,
  bodyMD: 15,
  bodySM: 13,
  monoLG: 11,
  monoSM: 10,
} as const;

// Tracking values applied via React Native's `letterSpacing` (in points, not em).
export const tracking = {
  displayTight: -1.6,
  displayNormal: -0.8,
  bodyNormal: 0,
  monoCaps: 1.1,
  buttonCaps: 0.6,
} as const;
