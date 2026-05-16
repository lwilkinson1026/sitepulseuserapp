import React from 'react';
import { StyleSheet, View, ViewStyle } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { colors, spacing } from '../theme';

// Base screen container — pure black background, safe-area aware,
// generous horizontal gutter that matches the spacing scale.

type ScreenProps = {
  children: React.ReactNode;
  edges?: ('top' | 'right' | 'bottom' | 'left')[];
  style?: ViewStyle;
  noGutter?: boolean;
};

export function Screen({ children, edges, style, noGutter }: ScreenProps) {
  return (
    <SafeAreaView
      edges={edges ?? ['top', 'left', 'right']}
      style={styles.safe}
    >
      <View style={[styles.inner, noGutter ? null : styles.gutter, style]}>
        {children}
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: {
    flex: 1,
    backgroundColor: colors.background,
  },
  inner: {
    flex: 1,
  },
  gutter: {
    paddingHorizontal: spacing.lg,
  },
});
