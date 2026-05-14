import { useState, useEffect, useCallback } from 'react';
import { RefreshCw, Settings } from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import { Toaster, toast } from 'sonner';

// Note: All styles are in index.css (Tailwind v4 + custom SitePulse design system)

// Type definitions matching the SitePulse Controller Architecture Spec
interface SystemStatus {
  battery_soc: number;           // 0-100 %
  battery_voltage: number;       // V
  battery_current: number;       // A (positive = discharge, negative = charge)
  battery_temp: number;          // °C
  inverter_power: number;        // W
  inverter_voltage: number;      // V (output)
  inverter_frequency: number;    // Hz
  inverter_load_percent: number; // 0-100
  outlets: Record<number, boolean>;
  last_update: string;           // ISO timestamp
  system_mode: 'battery_only' | 'charging' | 'hybrid' | 'error';
}

interface OutletConfig {
  id: number;
  name: string;
  icon?: string;
}

// Default outlet labels for a typical job-site setup (customizable later)
const DEFAULT_OUTLETS: OutletConfig[] = [
  { id: 1, name: 'Main Tools' },
  { id: 2, name: 'Site Lighting' },
  { id: 3, name: 'Battery Charger' },
  { id: 4, name: 'Welder / Plasma' },
  { id: 5, name: 'Site Office' },
  { id: 6, name: 'Spare / Misc' },
];

const DEMO_CONTROLLER_URL = 'https://demo.sitepulse.local';

function App() {
  // Connection state
  const [controllerUrl, setControllerUrl] = useState<string>('');
  const [isConnected, setIsConnected] = useState(false);
  const [isDemoMode, setIsDemoMode] = useState(true);
  const [showHardwareInput, setShowHardwareInput] = useState(false);

  // Live system state
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  // Polling interval (5s per spec recommendation)
  const POLL_INTERVAL = 5000;

  // Generate realistic demo data that slowly changes (simulates live hardware)
  const generateDemoStatus = useCallback((prev?: SystemStatus): SystemStatus => {
    const base = prev || {
      battery_soc: 78,
      battery_voltage: 50.8,
      battery_current: 8.4,
      battery_temp: 29.2,
      inverter_power: 1240,
      inverter_voltage: 229.8,
      inverter_frequency: 50.02,
      inverter_load_percent: 42,
      outlets: { 1: true, 2: true, 3: false, 4: true, 5: false, 6: false },
      last_update: new Date().toISOString(),
      system_mode: 'battery_only' as const,
    };

    // Small realistic fluctuations
    const socDelta = (Math.random() - 0.52) * 0.8;
    const newSoc = Math.max(12, Math.min(99, base.battery_soc + socDelta));

    const powerDelta = (Math.random() - 0.48) * 85;
    const newPower = Math.max(180, Math.min(2650, base.inverter_power + powerDelta));

    const loadDelta = (Math.random() - 0.5) * 3.5;
    const newLoad = Math.max(6, Math.min(94, base.inverter_load_percent + loadDelta));

    // Simulate charging when SOC is very low
    const isCharging = newSoc < 25 && Math.random() > 0.3;
    const current = isCharging 
      ? -(Math.random() * 18 + 9) 
      : (newPower / 48) * (0.92 + Math.random() * 0.12);

    return {
      battery_soc: Math.round(newSoc * 10) / 10,
      battery_voltage: Math.round((48 + (newSoc - 50) * 0.08 + (Math.random() - 0.5) * 0.6) * 10) / 10,
      battery_current: Math.round(current * 10) / 10,
      battery_temp: Math.round((base.battery_temp + (Math.random() - 0.5) * 0.6) * 10) / 10,
      inverter_power: Math.round(newPower),
      inverter_voltage: Math.round((230 + (Math.random() - 0.5) * 1.8) * 10) / 10,
      inverter_frequency: Math.round((50 + (Math.random() - 0.5) * 0.08) * 100) / 100,
      inverter_load_percent: Math.round(newLoad * 10) / 10,
      outlets: { ...base.outlets },
      last_update: new Date().toISOString(),
      system_mode: isCharging ? 'charging' : 'battery_only',
    };
  }, []);

  // Fetch status from controller (or simulate in demo mode)
  const fetchStatus = useCallback(async (url?: string): Promise<SystemStatus | null> => {
    const targetUrl = url || controllerUrl;

    if (!targetUrl || isDemoMode) {
      // DEMO MODE - smooth simulated hardware
      const newStatus = generateDemoStatus(status || undefined);
      return newStatus;
    }

    try {
      const res = await fetch(`${targetUrl.replace(/\/$/, '')}/status`, {
        method: 'GET',
        headers: { 'Accept': 'application/json' },
        signal: AbortSignal.timeout(6500),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      const data = await res.json();
      // Basic validation / normalization
      return {
        battery_soc: Number(data.battery_soc ?? 0),
        battery_voltage: Number(data.battery_voltage ?? 0),
        battery_current: Number(data.battery_current ?? 0),
        battery_temp: Number(data.battery_temp ?? 25),
        inverter_power: Number(data.inverter_power ?? 0),
        inverter_voltage: Number(data.inverter_voltage ?? 230),
        inverter_frequency: Number(data.inverter_frequency ?? 50),
        inverter_load_percent: Number(data.inverter_load_percent ?? 0),
        outlets: data.outlets || { 1: false, 2: false, 3: false, 4: false, 5: false, 6: false },
        last_update: data.last_update || new Date().toISOString(),
        system_mode: data.system_mode || 'battery_only',
      };
    } catch (err) {
      console.error('Fetch failed:', err);
      throw err;
    }
  }, [controllerUrl, isDemoMode, status, generateDemoStatus]);

  // Update a single outlet on the controller
  const toggleOutlet = async (channel: number, desiredState: boolean) => {
    if (isDemoMode) {
      // Instant local update in demo
      setStatus(prev => {
        if (!prev) return prev;
        const newOutlets = { ...prev.outlets, [channel]: desiredState };
        return { ...prev, outlets: newOutlets, last_update: new Date().toISOString() };
      });
      toast.success(`${DEFAULT_OUTLETS.find(o => o.id === channel)?.name} turned ${desiredState ? 'ON' : 'OFF'}`);
      return;
    }

    const base = controllerUrl.replace(/\/$/, '');
    setIsLoading(true);

    try {
      const res = await fetch(`${base}/outlets/${channel}/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ state: desiredState }),
      });

      if (!res.ok) throw new Error(`Failed to toggle: ${res.status}`);

      // Re-fetch fresh state
      const fresh = await fetchStatus();
      if (fresh) {
        setStatus(fresh);
      }
      toast.success(`Outlet ${channel} ${desiredState ? 'ON' : 'OFF'}`);
    } catch (err) {
      toast.error('Failed to control outlet. Check connection.');
      console.error(err);
    } finally {
      setIsLoading(false);
    }
  };

  // Connect to a controller URL (or enter demo)
  const connect = async (url: string, forceDemo = false) => {
    if (!url.trim() && !forceDemo) {
      toast.error('Please enter a controller URL or use Demo Mode');
      return;
    }

    setIsLoading(true);

    const cleanUrl = url.trim();
    const useDemo = forceDemo || cleanUrl === '' || cleanUrl.includes('demo');

    try {
      if (useDemo) {
        setIsDemoMode(true);
        setControllerUrl(DEMO_CONTROLLER_URL);
        const demoStatus = generateDemoStatus();
        setStatus(demoStatus);
        setIsConnected(true);
        localStorage.setItem('sitepulse_last_url', DEMO_CONTROLLER_URL);
        toast.success('Connected in Demo Mode — realistic live simulation');
      } else {
        setIsDemoMode(false);
        setControllerUrl(cleanUrl);
        
        const freshStatus = await fetchStatus(cleanUrl);
        if (freshStatus) {
          setStatus(freshStatus);
          setIsConnected(true);
          localStorage.setItem('sitepulse_last_url', cleanUrl);
          toast.success('Connected to SitePulse Controller');
        }
      }
    } catch (err) {
      // Fall back to demo so the user can still explore the UI
      setIsDemoMode(true);
      setControllerUrl(DEMO_CONTROLLER_URL);
      const demoStatus = generateDemoStatus();
      setStatus(demoStatus);
      setIsConnected(true);
      toast.error('Could not reach controller — running in Demo Mode');
    } finally {
      setIsLoading(false);
    }
  };

  // Manual refresh
  const refresh = async () => {
    if (!isConnected) return;
    setIsLoading(true);
    try {
      const fresh = await fetchStatus();
      if (fresh) {
        setStatus(fresh);
        if (!isDemoMode) toast.info('Status updated');
      }
    } catch (e) {
      toast.error('Refresh failed');
    } finally {
      setIsLoading(false);
    }
  };

  // Auto-polling effect (the heart of the live dashboard)
  useEffect(() => {
    if (!isConnected) return;

    const interval = setInterval(async () => {
      try {
        const fresh = await fetchStatus();
        if (fresh) {
          setStatus(fresh);
        }
      } catch {
        // silent fail in background polling
      }
    }, POLL_INTERVAL);

    return () => clearInterval(interval);
  }, [isConnected, fetchStatus, POLL_INTERVAL]);

  // Restore previous connection on load
  useEffect(() => {
    const saved = localStorage.getItem('sitepulse_last_url');
    if (saved) {
      // Auto-connect to last used controller (demo or real)
      const isDemo = saved === DEMO_CONTROLLER_URL || saved.includes('demo');
      connect(saved, isDemo);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ============================================
  // NEW MINIMALIST HIGH-END RENDER
  // ============================================

  // Helper — calm status color for the thin progress bar
  const getSocColor = (soc: number) => {
    if (soc < 15) return 'var(--sp-danger)';
    if (soc < 35) return 'var(--sp-warning)';
    return 'var(--sp-success)';
  };

  const socColor = status ? getSocColor(status.battery_soc) : 'var(--sp-accent)';
  const isLowSoc = status ? status.battery_soc < 20 : false;

  // ---------------------- CONNECTION SCREEN ----------------------
  if (!isConnected) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center px-6 bg-[#0b0c0f] text-white">
        <Toaster position="top-center" richColors closeButton />

        {/* Wordmark — quiet, expensive, technical */}
        <div className="text-center mb-16">
          <div className="text-[11px] tracking-[3px] text-[#52525b] font-medium mb-2">HYBRID POWER SYSTEM</div>
          <div className="text-[42px] font-semibold tracking-[-2.2px] text-white">SITEPULSE</div>
          <div className="text-[#52525b] text-sm mt-1 tracking-wide">Controller Interface</div>
        </div>

        <div className="w-full max-w-[320px] space-y-3">
          {/* Primary action — Demo (most people will use this first) */}
          <button
            onClick={() => connect('', true)}
            disabled={isLoading}
            className="btn btn-primary w-full text-base disabled:opacity-70"
          >
            Launch Demo Environment
          </button>

          {/* Secondary — real hardware */}
          <button
            onClick={() => setShowHardwareInput(!showHardwareInput)}
            className="btn btn-secondary w-full text-base"
          >
            Connect to Hardware
          </button>

          {/* Elegant URL input — only appears when needed */}
          <AnimatePresence>
            {showHardwareInput && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                className="pt-2 space-y-3"
              >
                <input
                  type="text"
                  className="input text-sm"
                  placeholder="https://xxxx.ngrok.io"
                  value={controllerUrl}
                  onChange={(e) => setControllerUrl(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && connect(controllerUrl)}
                  autoFocus
                />
                <button
                  onClick={() => connect(controllerUrl)}
                  disabled={isLoading || !controllerUrl.trim()}
                  className="btn btn-primary w-full disabled:opacity-50"
                >
                  {isLoading ? 'Connecting…' : 'Connect to Controller'}
                </button>
                <div className="text-center text-[10px] text-[#52525b] pt-1">
                  Enter the URL shown by ngrok on your Raspberry Pi
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        <div className="absolute bottom-8 text-[10px] text-[#3f4046] tracking-widest">
          v1.0  •  PHASE 1  •  BATTERY + INVERTER
        </div>
      </div>
    );
  }

  // ---------------------- DASHBOARD ----------------------
  if (!status) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[#0b0c0f] text-[#52525b] text-sm tracking-widest">
        CONNECTING TO CONTROLLER
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-[#0b0c0f] text-white pb-10">
      <Toaster position="top-center" richColors closeButton />

      {/* Ultra-minimal top bar — matches sitepulse.space restraint */}
      <div className="topbar">
        <div className="flex items-center gap-2">
          <span className="title">SITEPULSE</span>
          {isDemoMode && (
            <span className="text-[9px] px-1.5 py-px bg-[#1f222a] text-[#52525b] tracking-widest">DEMO</span>
          )}
        </div>

        <div className="flex items-center gap-3">
          <button onClick={refresh} disabled={isLoading} className="p-1 active:opacity-50 transition">
            <RefreshCw className={`w-3.5 h-3.5 text-[#52525b] ${isLoading ? 'animate-spin' : ''}`} />
          </button>
          <button
            onClick={() => {
              localStorage.removeItem('sitepulse_last_url');
              setIsConnected(false);
              setStatus(null);
              setControllerUrl('');
            }}
            className="p-1 active:opacity-50 transition"
          >
            <Settings className="w-3.5 h-3.5 text-[#52525b]" />
          </button>
        </div>
      </div>

      {/* UNIT IDENTIFIER + LIVE TELEMETRY — direct replication of sitepulse.space header style */}
      <div className="px-5 pt-4 pb-2">
        <div className="text-[10px] tracking-[1.5px] text-[#52525b] font-medium">
          UNIT-001 · BOZEMAN PLANT
        </div>

        <div className="flex items-center gap-2 mt-1 mb-3">
          <div className="text-[11px] tracking-[2px] text-[#a1a1aa] font-medium">LIVE TELEMETRY</div>
          <div className={`w-1 h-1 rounded-full ${isLowSoc ? 'bg-[#ef4444]' : 'bg-[#10b981]'} animate-pulse`} />
        </div>

        {/* Two primary live metrics — styled exactly like the marketing site */}
        <div className="grid grid-cols-2 gap-4">
          {/* Battery SOC — hero metric */}
          <div>
            <div className="text-[10px] tracking-[1px] text-[#52525b] mb-px">BATTERY SOC</div>
            <div className="flex items-baseline">
              <span 
                className="text-[56px] leading-none font-semibold tracking-[-3.2px] tabular-nums" 
                style={{ color: socColor }}
              >
                {status.battery_soc.toFixed(0)}
              </span>
              <span className="text-2xl text-[#52525b] ml-1">%</span>
            </div>
          </div>

          {/* Current Load / Power */}
          <div>
            <div className="text-[10px] tracking-[1px] text-[#52525b] mb-px">LOAD</div>
            <div className="flex items-baseline">
              <span className="text-[56px] leading-none font-semibold tracking-[-3.2px] tabular-nums">
                {status.inverter_power}
              </span>
              <span className="text-2xl text-[#52525b] ml-1">W</span>
            </div>
          </div>
        </div>
      </div>

      {/* Thin technical metadata strip — exactly like sitepulse.space */}
      <div className="px-5 py-2 text-[9px] tracking-[0.5px] text-[#52525b] border-y border-[#24262d] flex items-center gap-2 flex-wrap">
        <span>IP65</span>
        <span className="text-[#2a2d36]">·</span>
        <span>−20°C TO +50°C</span>
        <span className="text-[#2a2d36]">·</span>
        <span>CYCLE 2,418 / 6,000</span>
        <span className="text-[#2a2d36]">·</span>
        <span>~90 LB DRY</span>
      </div>

      {/* Supporting details — V / A / °C + thin SOC progress (kept minimal) */}
      <div className="px-5 pt-5">
        {/* Thin progress bar under the hero SOC (from LIVE TELEMETRY) */}
        <div className={`progress mb-5 ${isLowSoc ? 'danger' : ''}`}>
          <div 
            className="progress-bar" 
            style={{ width: `${Math.max(2, status.battery_soc)}%`, background: socColor }} 
          />
        </div>

        {/* Three supporting metrics in a very clean row — technical instrument style */}
        <div className="grid grid-cols-3 gap-2 text-center text-xs">
          <div className="bg-[#14151a] py-3 rounded-lg">
            <div className="text-[#52525b] tracking-widest text-[9px]">VOLTAGE</div>
            <div className="font-medium tabular-nums tracking-tight text-lg mt-0.5">{status.battery_voltage.toFixed(1)} <span className="text-[#52525b] text-xs">V</span></div>
          </div>
          <div className="bg-[#14151a] py-3 rounded-lg">
            <div className="text-[#52525b] tracking-widest text-[9px]">CURRENT</div>
            <div 
              className="font-medium tabular-nums tracking-tight text-lg mt-0.5"
              style={{ color: status.battery_current < 0 ? '#10b981' : undefined }}
            >
              {status.battery_current > 0 ? '+' : ''}{status.battery_current.toFixed(1)} <span className="text-[#52525b] text-xs">A</span>
            </div>
          </div>
          <div className="bg-[#14151a] py-3 rounded-lg">
            <div className="text-[#52525b] tracking-widest text-[9px]">TEMP</div>
            <div className="font-medium tabular-nums tracking-tight text-lg mt-0.5">{status.battery_temp.toFixed(1)} <span className="text-[#52525b] text-xs">°C</span></div>
          </div>
        </div>
      </div>

      {/* OUTLETS — clean, authoritative list */}
      <div className="section">
        <div className="flex items-baseline justify-between mb-2">
          <div className="text-label tracking-[1px]">AC OUTLETS</div>
          <div className="text-[10px] text-[#52525b]">6 RELAY CHANNELS</div>
        </div>

        <div className="surface rounded-2xl px-5 divide-y divide-[var(--sp-border)]">
          {DEFAULT_OUTLETS.map((outlet) => {
            const isOn = status.outlets[outlet.id] ?? false;
            return (
              <div key={outlet.id} className="outlet">
                <div>
                  <div className="label">{outlet.name}</div>
                  <div className="meta">Channel {outlet.id}</div>
                </div>

                <button
                  onClick={() => toggleOutlet(outlet.id, !isOn)}
                  disabled={isLoading}
                  className={`switch ${isOn ? 'active' : ''}`}
                  aria-label={`Toggle ${outlet.name}`}
                />
              </div>
            );
          })}
        </div>

        <div className="text-center text-[10px] text-[#3f4046] mt-4 tracking-widest">
          COMMANDS SENT TO RASPBERRY PI GPIO
        </div>
      </div>

      {/* Footer status — calm and minimal */}
      <div className="px-6 pt-4 text-center">
        <button
          onClick={() => {
            localStorage.removeItem('sitepulse_last_url');
            setIsConnected(false);
            setStatus(null);
          }}
          className="text-[#3f4046] text-xs tracking-widest active:text-[#52525b] transition-colors"
        >
          DISCONNECT CONTROLLER
        </button>
      </div>
    </div>
  );
}

export default App;
