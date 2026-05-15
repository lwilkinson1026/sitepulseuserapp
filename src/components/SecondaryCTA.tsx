import React from 'react';
import { Pressable, StyleSheet, Text, View, ViewStyle } from 'react-native';
import { colors, fonts, hairline, spacing, tracking, typeScale } from '../theme';

// Transparent fill, hairline white border, white text, trailing arrow.
// Secondary actions and reversible options.

type SecondaryCTAProps = {
  label: string;
  onPress?: () => void;
  disabled?: boolean;
  style?: ViewStyle;
};

export function SecondaryCTA({ label, onPress, disabled, style }: SecondaryCTAProps) {
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
    backgroundColor: 'transparent',
    borderWidth: hairline,
    borderColor: colors.textDisplay,
    borderRadius: 0,
    paddingVertical: spacing.md,
    paddingHorizontal: spacing.lg,
  },
  buttonPressed: {
    opacity: 0.7,
  },
  buttonDisabled: {
    opacity: 0.35,
  },
  inner: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  label: {
    color: colors.textDisplay,
    fontFamily: fonts.bodyMedium,
    fontSize: typeScale.bodyMD,
    letterSpacing: tracking.buttonCaps,
  },
  arrow: {
    color: colors.textDisplay,
    fontFamily: fonts.bodyMedium,
    fontSize: typeScale.titleMD,
    marginLeft: spacing.md,
  },
});
