// Centralized param-list types for type-safe navigation.
// Keep these in lockstep with the navigators they describe.

export type AuthStackParamList = {
  Splash: undefined;
  SignIn: undefined;
  SignUp: undefined;
};

export type PairStackParamList = {
  EmptyState: undefined;
  // QrPair: undefined;       // phase 1, after design system
  // PairConfirming: { token: string };
  // PairSuccess: { unitId: string };
};

export type MainTabsParamList = {
  Dashboard: undefined;
  Outlets: undefined;
  Schedule: undefined;
  Activity: undefined;
};
