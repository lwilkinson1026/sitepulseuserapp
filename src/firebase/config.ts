// Firebase initialization for the SitePulse mobile app.
//
// Real credentials should land in a `.env` (loaded via Expo's
// EXPO_PUBLIC_* convention) once the Firebase project is provisioned
// per spec §11 Phase 0. For now we read from process.env and fall back
// to placeholders so the build stays green during the design-system pass.

import { Platform } from 'react-native';
import { initializeApp, getApps, getApp, FirebaseApp } from 'firebase/app';
// @ts-expect-error — getReactNativePersistence is exported but missing from the .d.ts in firebase 12.x
import { getAuth, initializeAuth, getReactNativePersistence, Auth } from 'firebase/auth';
import { getFirestore, Firestore } from 'firebase/firestore';
import AsyncStorage from '@react-native-async-storage/async-storage';

const firebaseConfig = {
  apiKey: process.env.EXPO_PUBLIC_FIREBASE_API_KEY ?? 'placeholder-api-key',
  authDomain: process.env.EXPO_PUBLIC_FIREBASE_AUTH_DOMAIN ?? 'placeholder.firebaseapp.com',
  projectId: process.env.EXPO_PUBLIC_FIREBASE_PROJECT_ID ?? 'sitepulse-placeholder',
  storageBucket: process.env.EXPO_PUBLIC_FIREBASE_STORAGE_BUCKET ?? 'sitepulse-placeholder.appspot.com',
  messagingSenderId: process.env.EXPO_PUBLIC_FIREBASE_MESSAGING_SENDER_ID ?? '000000000000',
  appId: process.env.EXPO_PUBLIC_FIREBASE_APP_ID ?? '1:000000000000:web:placeholder',
};

let app: FirebaseApp;
let auth: Auth;
let db: Firestore;

export function ensureFirebase() {
  if (getApps().length === 0) {
    app = initializeApp(firebaseConfig);
    if (Platform.OS === 'web') {
      // Web build uses the default IndexedDB persistence; getReactNativePersistence isn't exported.
      auth = getAuth(app);
    } else {
      auth = initializeAuth(app, {
        persistence: getReactNativePersistence(AsyncStorage),
      });
    }
  } else {
    app = getApp();
    auth = getAuth(app);
  }
  db = getFirestore(app);
  return { app, auth, db };
}

export function getFirebase() {
  if (!app) ensureFirebase();
  return { app, auth, db };
}
