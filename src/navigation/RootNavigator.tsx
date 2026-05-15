import React from 'react';
import { ActivityIndicator, StyleSheet, View } from 'react-native';
import { NavigationContainer, DefaultTheme } from '@react-navigation/native';
import { AuthStack } from './AuthStack';
import { MainTabs } from './MainTabs';
import { useAuth } from '../hooks/AuthContext';
import { colors } from '../theme';

// Top-level switch: while Firebase resolves the persisted session show a
// minimal loading state; afterward route to the auth flow or the main tabs.

const navTheme = {
  ...DefaultTheme,
  dark: true,
  colors: {
    ...DefaultTheme.colors,
    background: colors.background,
    card: colors.background,
    text: colors.textDisplay,
    border: colors.borderHairline,
    primary: colors.textDisplay,
    notification: colors.live,
  },
};

export function RootNavigator() {
  const { initializing, user } = useAuth();

  if (initializing) {
    return (
      <View style={styles.loading}>
        <ActivityIndicator color={colors.textMuted} />
      </View>
    );
  }

  return (
    <NavigationContainer theme={navTheme}>
      {user ? <MainTabs /> : <AuthStack />}
    </NavigationContainer>
  );
}

const styles = StyleSheet.create({
  loading: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colors.background,
  },
});
