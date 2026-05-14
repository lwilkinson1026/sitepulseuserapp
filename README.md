# SitePulse User App

**Mobile-first PWA for the SitePulse Hybrid Job-Site Power & Connectivity System.**

Real-time monitoring and control of the Raspberry Pi controller that manages battery banks, hybrid inverters, and AC outlets on construction sites.

## Features (v0.1)

- **Live Battery Dashboard** — Large animated SOC gauge + voltage, current (charge/discharge), temperature
- **Inverter Telemetry** — Output power (W), load %, voltage, frequency with live updating bars
- **Remote Outlet Control** — 6 labeled AC channels with instant toggle switches (maps to GPIO relays on the Pi)
- **Demo Mode** — Fully functional realistic simulation so you can test and design the UI while the hardware is being built (3D printed enclosure, wiring, VESC integration)
- **Direct Controller API** — Connects to the FastAPI server running on the Pi (via ngrok or local network)
- **Mobile optimized** — Large touch targets, high-contrast outdoor-friendly theme, works great as a PWA on iPhone/Android

## Architecture Alignment

This app is the official companion to the [SitePulse Controller Software Architecture Specification v1.0](/Users/test/Downloads/SitePulse_Controller_Software_Architecture_Specification_v1.docx).

It consumes:
- `GET /status` → `SystemStatus` JSON (primary polling endpoint)
- `POST /outlets/{channel}/toggle` → `{ "state": boolean }`

Future phases will add:
- WebSocket `/ws/status` for push updates
- Historical power logs + charts
- Firebase-backed multi-site / multi-user management
- QR code pairing flow
- VESC motor controller (generator auto-start) commands

## Quick Start

```bash
cd sitepulseuserapp
npm install
npm run dev
```

Open on your phone (or desktop). Use **Demo Mode** first — it generates live fluctuating data exactly like a real 48V LiFePO4 + 2000W inverter setup.

To connect to real hardware:
1. Flash the FastAPI controller (see controller repo / spec)
2. Run `ngrok http 8000` on the Pi (or use the built-in ngrok Python SDK)
3. Enter the resulting `https://xxxx.ngrok.io` URL in the app

## Tech Stack

- Vite + React 19 + TypeScript
- Tailwind CSS v4 (mobile-first, dark mode ready)
- Framer Motion (smooth gauge + load bar animations)
- Lucide icons
- Sonner (beautiful toasts)
- date-fns

Designed to be installable as a PWA (Add to Home Screen) for field use.

## Development Notes

- The app gracefully falls back to Demo Mode if the controller is unreachable.
- All outlet names and the number of channels are easily configurable in `src/App.tsx`.
- Polling interval is currently hardcoded to 5s (per spec recommendation).

## Next Steps / Roadmap

1. Add real WebSocket support (`/ws/status`)
2. Settings screen (polling rate, outlet name editing, basic auth credentials)
3. Offline queue for commands when signal is poor on site
4. Firebase integration (central telemetry + push notifications for "low battery", "overload")
5. QR code scanner for instant pairing with a controller
6. Power history charts (Recharts or native SVG)
7. Native builds (Capacitor or Tauri) if PWA isn't sufficient for App Store distribution

---

**"Ready when you are — just say the word and I’ll generate the starter code files..."** — the spec was right.

Let's build the full stack: controller + this user app + beautiful 3D-printed stainless enclosure + VESC generator auto-start logic.

## License

Internal / proprietary for now.
      tseslint.configs.stylisticTypeChecked,

      // Other configs...
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])
```

You can also install [eslint-plugin-react-x](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-x) and [eslint-plugin-react-dom](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-dom) for React-specific lint rules:

```js
// eslint.config.js
import reactX from 'eslint-plugin-react-x'
import reactDom from 'eslint-plugin-react-dom'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...
      // Enable lint rules for React
      reactX.configs['recommended-typescript'],
      // Enable lint rules for React DOM
      reactDom.configs.recommended,
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])
```
