// SitePulse Cloud Functions entry point.
//
// Exports are kept flat so `firebase deploy --only functions:onEventCreated`
// works without subpath gymnastics.

export { onEventCreated } from './pushFanout';
