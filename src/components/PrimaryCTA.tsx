import React from 'react';
import { Pressable, StyleSheet, Text, View, ViewStyle } from 'react-native';
import { colors, fonts, spacing, tracking, typeScale } from '../theme';

// White-block CTA with trailing right arrow. Direct visual quote from
// the marketing site's "RESERVE →" treatment. Square corners. Uppercase.
// Used for primary actions: "PAIR UNIT →", "ARM THEFT MODE →", "SIGN IN →".

type PrimaryCTAProps = {
  label: string;
  onPress?: () => void;
  disabled?: boolean;
  style?: ViewStyle;
};

export function PrimaryCTA({ label, onPress, disabled, style }: PrimaryCTAProps) {
  return (
    <Pressable
      onPress={disabled ? undefined : onPress}
      style={({ pressed }) => [
        styles.button,
        pressed && !disabled ? styles.buttonPressed : null,
        disabled ? styles.buttonDisabled : null,
        style,
      ]}
      accessibilityRole="button"
      accessibilityLabel={label}
    >
      <View style={styles.inner}>
        <Text style={styles.label}>{label.toUpperCase()}</Text>
        <Text style={styles.arrow}>→</Text>
      </View>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  button: {
    backgroundColor: colors.ctaFill,
    borderRadius: 0,           // square corners — brand rule
    paddingVertical: spacing.md,
    paddingHorizontal: spacing.lg,
  },
  buttonPressed: {
    opacity: 0.85,
  },
  buttonDisabled: {
    opacity: 0.4,
  },
  inner: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  label: {
    color: colors.ctaText,
    fontFamily: fonts.bodySemibold,
    fontSize: typeScale.bodyMD,
    letterSpacing: tracking.buttonCaps,
  },
  arrow: {
    color: colors.ctaText,
    fontFamily: fonts.bodySemibold,
    fontSize: typeScale.titleMD,
    marginLeft: spacing.md,
  },
});
