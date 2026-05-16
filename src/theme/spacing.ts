// Per spec §10.4 — spacing scale and corner radius.
// Corners are square-first: 0 for buttons/cards/inputs, 4 max where unavoidable,
// 12 reserved for full-screen modal sheets only.

export const spacing = {
  xxs: 4,
  xs: 8,
  sm: 12,
  md: 16,
  lg: 24,
  xl: 32,
  xxl: 48,
} as const;

export const radius = {
  square: 0,
  hint: 4,
  sheet: 12,
} as const;

export const hairline = 1;
