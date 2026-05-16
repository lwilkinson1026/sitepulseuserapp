// Register an Expo Push token under users/{uid}/pushTokens/{token} on sign-in.
//
// Runs once per signed-in session; the Cloud Function `onEventCreated` reads
// these tokens to fan out notifications when an event is logged for a unit
// the user owns.
//
// Notes:
//   - Web has no Expo Push support; we no-op there.
//   - Pushes only work on a physical device (Simulator returns no token).
//   - Token doc id IS the token string, so re-registering on the same device
//     overwrites cleanly with no duplicates.

import { useEffect, useRef } from 'react';
import { Platform } from 'react-native';
import * as Notifications from 'expo-notifications';
import * as Device from 'expo-device';
import Constants from 'expo-constants';
import {
  doc,
  serverTimestamp,
  setDoc,
} from 'firebase/firestore';
import type { User } from 'firebase/auth';
import { getFirebase } from '../firebase/config';

// Foreground notification presentation. Banner + sound, no badge for now.
Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowBanner: true,
    shouldShowList: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
  }),
});

async function fetchExpoPushToken(): Promise<string | null> {
  if (Platform.OS === 'web') return null;
  if (!Device.isDevice) return null;        // simulators yield no token

  const existing = await Notifications.getPermissionsAsync();
  let status = existing.status;
  if (status !== 'granted') {
    const req = await Notifications.requestPermissionsAsync();
    status = req.status;
  }
  if (status !== 'granted') return null;

  // EAS projectId is the binding key for an ExponentPushToken in production
  // builds. Constants exposes it under expoConfig.extra.eas.projectId.
  const projectId =
    Constants.expoConfig?.extra?.eas?.projectId ??
    (Constants as { easConfig?: { projectId?: string } }).easConfig?.projectId;

  const tokenResp = await Notifications.getExpoPushTokenAsync(
    projectId ? { projectId } : undefined,
  );
  return tokenResp.data;
}

export function usePushTokenRegistration(user: User | null) {
  // Track which uid+token pair we've already written this session so a
  // re-render doesn't trigger an unnecessary Firestore write on every tick.
  const lastWritten = useRef<string | null>(null);

  useEffect(() => {
    if (!user) {
      lastWritten.current = null;
      return;
    }

    let cancelled = false;

    (async () => {
      try {
        const token = await fetchExpoPushToken();
        if (!token || cancelled) return;

        const key = `${user.uid}:${token}`;
        if (lastWritten.current === key) return;

        const { db } = getFirebase();
        const ref = doc(db, 'users', user.uid, 'pushTokens', token);
        await setDoc(
          ref,
          {
            token,
            platform: Platform.OS as 'ios' | 'android' | 'web',
            deviceName: Device.deviceName ?? null,
            createdAt: serverTimestamp(),
            lastSeenAt: serverTimestamp(),
          },
          { merge: true },
        );
        lastWritten.current = key;
      } catch (err) {
        // Push registration is best-effort. Don't crash the app over it.
        console.warn('[push] registration failed:', err);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [user]);
}
