import React from 'react';
import { StyleSheet, View, ViewStyle } from 'react-native';
import { colors, hairline } from '../theme';

// 1-px L-shaped marks at the four corners of imagery, dashboard hero cards,
// and the camera viewfinder during QR pairing. Each leg ~12 px.
// Wrap children to draw the brackets around them.

type CornerBracketsProps = {
  size?: number;             // leg length in px (default 12)
  color?: string;            // override hairline color (e.g. white on dark)
  inset?: number;            // distance from edge (default 0)
  children?: React.ReactNode;
  style?: ViewStyle;
};

export function CornerBrackets({
  size = 12,
  color = colors.borderStrong,
  inset = 0,
  children,
  style,
}: CornerBracketsProps) {
  return (
    <View style={[styles.wrap, style]}>
      {children}
      <Bracket position="tl" size={size} color={color} inset={inset} />
      <Bracket position="tr" size={size} color={color} inset={inset} />
      <Bracket position="bl" size={size} color={color} inset={inset} />
      <Bracket position="br" size={size} color={color} inset={inset} />
    </View>
  );
}

function Bracket({
  position,
  size,
  color,
  inset,
}: {
  position: 'tl' | 'tr' | 'bl' | 'br';
  size: number;
  color: string;
  inset: number;
}) {
  const isTop = position[0] === 't';
  const isLeft = position[1] === 'l';
  const horizontalBorder: ViewStyle = isTop
    ? { borderTopWidth: hairline, borderTopColor: color }
    : { borderBottomWidth: hairline, borderBottomColor: color };
  const verticalBorder: ViewStyle = isLeft
    ? { borderLeftWidth: hairline, borderLeftColor: color }
    : { borderRightWidth: hairline, borderRightColor: color };

  return (
    <View
      style={[
        styles.bracket,
        { width: size, height: size },
        isTop ? { top: inset } : { bottom: inset },
        isLeft ? { left: inset } : { right: inset },
        horizontalBorder,
        verticalBorder,
      ]}
    />
  );
}

const styles = StyleSheet.create({
  wrap: {
    position: 'relative',
  },
  bracket: {
    position: 'absolute',
    pointerEvents: 'none',
  },
});
