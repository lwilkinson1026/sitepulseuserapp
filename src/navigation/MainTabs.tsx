import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { DashboardScreen } from '../screens/tabs/DashboardScreen';
import { OutletsScreen } from '../screens/tabs/OutletsScreen';
import { ScheduleScreen } from '../screens/tabs/ScheduleScreen';
import { ActivityScreen } from '../screens/tabs/ActivityScreen';
import { colors, fonts, hairline, tracking, typeScale } from '../theme';
import { MainTabsParamList } from './types';

const Tab = createBottomTabNavigator<MainTabsParamList>();

// Tab labels follow the "01 / DASHBOARD" pattern from spec §10.3, lifted
// directly from the marketing site's "01 / BATTERY · 02 / CONNECTIVITY · …".

const TAB_LABELS: Record<keyof MainTabsParamList, string> = {
  Dashboard: '01 / DASHBOARD',
  Outlets: '02 / OUTLETS',
  Schedule: '03 / SCHEDULE',
  Activity: '04 / ACTIVITY',
};

function TabLabel({ routeName, focused }: { routeName: keyof MainTabsParamList; focused: boolean }) {
  return (
    <Text
      style={[
        styles.tabLabel,
        focused ? styles.tabLabelFocused : null,
      ]}
      numberOfLines={1}
    >
      {TAB_LABELS[routeName]}
    </Text>
  );
}

export function MainTabs() {
  return (
    <Tab.Navigator
      screenOptions={({ route }) => ({
        headerShown: false,
        tabBarShowLabel: true,
        tabBarStyle: styles.tabBar,
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
      <Tab.Screen name="Activity" component={ActivityScreen} />
    </Tab.Navigator>
  );
}

const styles = StyleSheet.create({
  tabBar: {
    backgroundColor: colors.background,
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
    height: 64,
    paddingTop: 6,
    paddingBottom: 8,
  },
  tabItem: {
    paddingVertical: 0,
  },
  tabLabel: {
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
    color: colors.textMuted,
  },
  tabLabelFocused: {
    color: colors.textDisplay,
  },
  unused: {
    display: 'none',
  } as View['props'],
});
