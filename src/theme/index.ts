export { colors } from './colors';
export { spacing, radius, hairline } from './spacing';
export { fonts, typeScale, tracking } from './typography';

export const motion = {
  // Spec §10.5 — 180–220 ms ease, industrial feel, no spring/bounce.
  durationStandard: 200,
  easingStandard: [0.2, 0.8, 0.2, 1] as const,
  // Live dot 1.6 s breath cycle.
  livePulse: 1600,
} as const;
