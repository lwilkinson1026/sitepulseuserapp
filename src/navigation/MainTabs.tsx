import React from 'react';
import { Platform, StyleSheet, Text, View } from 'react-native';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { DashboardScreen } from '../screens/tabs/DashboardScreen';
import { OutletsScreen } from '../screens/tabs/OutletsScreen';
import { ScheduleScreen } from '../screens/tabs/ScheduleScreen';
import { SentryScreen } from '../screens/tabs/SentryScreen';
import { ActivityScreen } from '../screens/tabs/ActivityScreen';
import { EngineStatusBar } from '../components/EngineStatusBar';
import { colors, fonts, hairline } from '../theme';
import { MainTabsParamList } from './types';

const Tab = createBottomTabNavigator<MainTabsParamList>();

// Tab labels follow the "01 / DASHBOARD" pattern from spec §10.3, lifted
// directly from the marketing site's "01 / BATTERY · 02 / CONNECTIVITY · …".
// We stack the section number above the label so the full word fits at a
// readable size — no mid-word truncation, no eye-straining 9-pt micro type.

const TAB_LABELS: Record<keyof MainTabsParamList, { num: string; label: string }> = {
  Dashboard: { num: '01', label: 'DASHBOARD' },
  Outlets:   { num: '02', label: 'OUTLETS' },
  Schedule:  { num: '03', label: 'SCHEDULE' },
  Sentry:    { num: '04', label: 'SENTRY' },
  Activity:  { num: '05', label: 'ACTIVITY' },
};

function TabLabel({ routeName, focused }: { routeName: keyof MainTabsParamList; focused: boolean }) {
  const { num, label } = TAB_LABELS[routeName];
  return (
    <View style={styles.tabLabelCol}>
      <Text
        style={[styles.tabNum, focused ? styles.tabNumFocused : null]}
        numberOfLines={1}
      >
        {num}
      </Text>
      <Text
        style={[styles.tabLabel, focused ? styles.tabLabelFocused : null]}
        numberOfLines={1}
      >
        {label}
      </Text>
    </View>
  );
}

// iOS Safari's URL bar (~50 px tall) is NOT reported via
// safe-area-inset-bottom while it's visible, so insets.bottom = 0 on the
// browser web build and the tab bar gets covered. Reserve a hardcoded
// floor on web large enough to clear it. A standalone PWA (display-mode
// installed-app) gets the real home-indicator inset instead — never
// double-pads. Native builds keep the existing behavior.
const WEB_BROWSER_BOTTOM_FLOOR = 90;

function getWebBottomFloor(): number {
  if (Platform.OS !== 'web') return 8;
  if (typeof window === 'undefined' || !window.matchMedia) return WEB_BROWSER_BOTTOM_FLOOR;
  const isStandalone = window.matchMedia('(display-mode: standalone)').matches;
  return isStandalone ? 8 : WEB_BROWSER_BOTTOM_FLOOR;
}

export function MainTabs() {
  // Respect the home-indicator safe area so labels aren't clipped on
  // notched iPhones and the touch target sits above the system gesture bar.
  const insets = useSafeAreaInsets();
  const bottomInset = Math.max(insets.bottom, getWebBottomFloor());

  return (
    <View style={styles.root}>
      {/* Engine status banner — only renders when state ≠ 'idle'.
          Owns its own top safe-area inset; when visible, child screens'
          SafeAreaView edges=['top'] receive 0 inset (because the parent
          already consumed it), so layout flows naturally. */}
      <EngineStatusBar />

      <Tab.Navigator
        screenOptions={({ route }) => ({
          headerShown: false,
          tabBarShowLabel: true,
          tabBarStyle: [styles.tabBar, { height: 64 + bottomInset, paddingBottom: bottomInset }],
          tabBarItemStyle: styles.tabItem,
          tabBarIcon: () => null,
          tabBarLabel: ({ focused }) => (
            <TabLabel routeName={route.name as keyof MainTabsParamList} focused={focused} />
          ),
          sceneStyle: { backgroundColor: colors.background },
        })}
      >
        <Tab.Screen name="Dashboard" component={DashboardScreen} />
        <Tab.Screen name="Outlets" component={OutletsScreen} />
        <Tab.Screen name="Schedule" component={ScheduleScreen} />
        <Tab.Screen name="Sentry" component={SentryScreen} />
        <Tab.Screen name="Activity" component={ActivityScreen} />
      </Tab.Navigator>
    </View>
  );
}

const styles = StyleSheet.create({
  root: {
    flex: 1,
    backgroundColor: colors.background,
  },
  tabBar: {
    backgroundColor: colors.background,
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
    paddingTop: 10,
    paddingHorizontal: 0,
  },
  tabItem: {
    paddingVertical: 0,
    paddingHorizontal: 2,
  },
  tabLabelCol: {
    alignItems: 'center',
    gap: 4,
  },
  tabNum: {
    fontFamily: fonts.mono,
    fontSize: 10,
    letterSpacing: 1,
    color: colors.textMuted,
  },
  tabNumFocused: {
    color: colors.textBody,
  },
  tabLabel: {
    fontFamily: fonts.mono,
    fontSize: 12,
    letterSpacing: 1,
    color: colors.textMuted,
  },
  tabLabelFocused: {
    color: colors.textDisplay,
  },
});
