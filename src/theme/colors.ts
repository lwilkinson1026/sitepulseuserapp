// SitePulse v2.1 brand palette — pure monochrome, anchored on sitepulse.space.
// Green appears ONLY as a "live" status dot. Never as fill or primary color.

export const colors = {
  background: '#000000',        // pure black, matches marketing site exactly
  surface: '#0A0A0C',           // lifted card only when necessary
  surfaceElevated: '#141416',   // sheets, modals, overlay surfaces

  borderHairline: '#1F1F22',
  borderStrong: '#2F2F33',

  textDisplay: '#FFFFFF',       // hero numerics, headlines, CTA glyphs
  textBody: '#A1A1A6',          // descriptive copy, secondary labels
  textMuted: '#52525B',         // eyebrows, FIG. captions, separator dots

  live: '#10B981',              // semantic only — pulsing live dot
  warning: '#F59E0B',           // stale telemetry, low fuel, blocked engine start
  danger: '#EF4444',            // faults, critical SOC, theft-mode armed

  ctaFill: '#FFFFFF',
  ctaText: '#000000',
} as const;

export type ColorToken = keyof typeof colors;
