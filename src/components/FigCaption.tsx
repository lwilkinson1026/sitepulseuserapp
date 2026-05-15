import React from 'react';
import { StyleSheet, Text } from 'react-native';
import { colors, fonts, tracking, typeScale } from '../theme';

// "FIG. 01 · DASHBOARD · UNIT-001" — the brand's technical-document voice.
// Every screen with imagery or a hero metric carries one of these.

type FigCaptionProps = {
  number: number | string;
  label: string;
  detail?: string;
};

export function FigCaption({ number, label, detail }: FigCaptionProps) {
  const numStr = typeof number === 'number' ? String(number).padStart(2, '0') : number;
  const parts = [`FIG. ${numStr}`, label.toUpperCase()];
  if (detail) parts.push(detail.toUpperCase());
  return <Text style={styles.text}>{parts.join('  ·  ')}</Text>;
}

const styles = StyleSheet.create({
  text: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
});
