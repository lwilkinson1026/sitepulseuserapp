import { useState, useEffect, useCallback } from 'react';
import { Battery, Zap, Clock, RefreshCw, Power, Wifi, WifiOff, Settings, AlertTriangle } from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import { Toaster, toast } from 'sonner';
import { formatDistanceToNow } from 'date-fns';

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

  // Live system state
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [lastSync, setLastSync] = useState<Date | null>(null);

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
        setLastSync(new Date());
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
        setLastSync(new Date());
        localStorage.setItem('sitepulse_last_url', DEMO_CONTROLLER_URL);
        toast.success('Connected in Demo Mode — realistic live simulation');
      } else {
        setIsDemoMode(false);
        setControllerUrl(cleanUrl);
        
        const freshStatus = await fetchStatus(cleanUrl);
        if (freshStatus) {
          setStatus(freshStatus);
          setIsConnected(true);
          setLastSync(new Date());
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
      setLastSync(new Date());
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
        setLastSync(new Date());
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
          setLastSync(new Date());
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

  // Helper to get SOC color
  const getSocColor = (soc: number) => {
    if (soc > 55) return 'var(--sp-success)';
    if (soc > 25) return 'var(--sp-warning)';
    return 'var(--sp-danger)';
  };

  const socColor = status ? getSocColor(status.battery_soc) : 'var(--sp-primary)';

  // Render the beautiful SOC circular gauge
  const renderSOCGauge = (soc: number) => {
    const radius = 78;
    const circumference = 2 * Math.PI * radius;
    const offset = circumference * (1 - soc / 100);

    return (
      <div className="soc-gauge mx-auto">
        <svg width="180" height="180" viewBox="0 0 180 180" className="drop-shadow-sm">
          {/* Background track */}
          <circle
            cx="90" cy="90" r={radius}
            fill="none"
            stroke="var(--sp-border)"
            strokeWidth="14"
          />
          {/* Progress arc */}
          <motion.circle
            cx="90" cy="90" r={radius}
            fill="none"
            stroke={socColor}
            strokeWidth="14"
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            initial={{ strokeDashoffset: circumference }}
            animate={{ strokeDashoffset: offset }}
            transition={{ duration: 0.6, ease: 'easeOut' }}
          />
        </svg>
        <div className="value">
          <div className="text-6xl font-bold tabular-nums tracking-tighter" style={{ color: socColor }}>
            {soc.toFixed(0)}
          </div>
          <div className="text-sm font-medium text-[var(--sp-text-muted)] -mt-1">SOC %</div>
        </div>
      </div>
    );
  };

  // ---------------------- RENDER ----------------------

  // CONNECTION / ONBOARDING SCREEN
  if (!isConnected) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center p-6 bg-[var(--sp-bg)]">
        <Toaster position="top-center" richColors closeButton />

        <div className="w-full max-w-sm text-center mb-8">
          <div className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-[var(--sp-primary)] mb-4">
            <Power className="w-9 h-9 text-white" />
          </div>
          <h1 className="text-4xl font-semibold tracking-tight text-[var(--sp-text)]">SitePulse</h1>
          <p className="text-lg text-[var(--sp-text-muted)] mt-1">Hybrid Job-Site Power</p>
          <p className="text-sm text-[var(--sp-text-subtle)] mt-2">Real-time control from anywhere on site</p>
        </div>

        <div className="w-full max-w-sm space-y-4">
          <div className="card">
            <div className="text-sm font-semibold mb-3 text-[var(--sp-text-muted)]">CONNECT TO CONTROLLER</div>
            
            <input
              type="text"
              className="input mb-3 font-mono text-sm"
              placeholder="https://your-site.ngrok.io"
              value={controllerUrl}
              onChange={(e) => setControllerUrl(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && connect(controllerUrl)}
            />

            <button
              onClick={() => connect(controllerUrl)}
              disabled={isLoading}
              className="btn-primary w-full disabled:opacity-60"
            >
              {isLoading ? 'Connecting…' : 'Connect to Controller'}
            </button>

            <div className="text-center my-3 text-xs text-[var(--sp-text-subtle)]">or</div>

            <button
              onClick={() => connect('', true)}
              disabled={isLoading}
              className="btn-secondary w-full flex items-center justify-center gap-2"
            >
              <Battery className="w-4 h-4" /> Launch Demo Mode
            </button>
          </div>

          <div className="text-[10px] text-center text-[var(--sp-text-subtle)] leading-snug px-4">
            Demo Mode shows realistic live battery + inverter data.<br />
            In production you will enter the ngrok (or custom domain) URL shown on your Raspberry Pi.
          </div>
        </div>

        <div className="mt-auto pt-8 text-[10px] text-[var(--sp-text-subtle)]">
          v1.0 • Phase 1 (Battery + Inverter) • Matches Controller Spec
        </div>
      </div>
    );
  }

  // MAIN DASHBOARD
  if (!status) {
    return <div className="min-h-screen flex items-center justify-center">Loading system status...</div>;
  }

  const isCharging = status.system_mode === 'charging';
  const currentDirection = status.battery_current >= 0 ? 'discharging' : 'charging';

  return (
    <div className="min-h-screen pb-8 bg-[var(--sp-bg)]">
      <Toaster position="top-center" richColors closeButton />

      {/* Top connection bar */}
      <div className="connection-banner px-5 py-3 flex items-center justify-between text-sm sticky top-0 z-50">
        <div className="flex items-center gap-2.5">
          {isDemoMode ? (
            <WifiOff className="w-4 h-4" />
          ) : (
            <Wifi className="w-4 h-4" />
          )}
          <div>
            <div className="font-semibold tracking-tight">SitePulse Controller</div>
            <div className="text-[10px] opacity-80 -mt-0.5 font-mono truncate max-w-[180px]">
              {isDemoMode ? 'DEMO — simulated hardware' : controllerUrl.replace(/^https?:\/\//, '')}
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <div className={`status-dot ${status.battery_soc < 20 ? 'danger' : status.battery_soc < 45 ? 'warning' : ''}`} />
          <button
            onClick={refresh}
            disabled={isLoading}
            className="p-2 rounded-full hover:bg-white/10 active:bg-white/20 transition"
          >
            <RefreshCw className={`w-4 h-4 ${isLoading ? 'animate-spin' : ''}`} />
          </button>
          <button
            onClick={() => {
              setIsConnected(false);
              setStatus(null);
              setControllerUrl('');
            }}
            className="p-2 rounded-full hover:bg-white/10 active:bg-white/20 transition"
            title="Disconnect"
          >
            <Settings className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Demo banner */}
      <AnimatePresence>
        {isDemoMode && (
          <div className="bg-amber-100 dark:bg-amber-950/40 text-amber-800 dark:text-amber-200 text-xs px-4 py-2 flex items-center gap-2 border-b border-amber-200 dark:border-amber-900">
            <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0" />
            <span>Demo mode — data is simulated in real time. Connect a real controller to control live hardware.</span>
          </div>
        )}
      </AnimatePresence>

      <div className="p-5 space-y-5 max-w-[480px] mx-auto">
        {/* BATTERY SECTION */}
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <Battery className="w-5 h-5 text-[var(--sp-primary)]" />
              <div className="font-semibold tracking-tight">Battery Bank</div>
            </div>
            <div className="text-xs px-3 py-0.5 rounded-full font-medium" 
                 style={{ background: `${socColor}20`, color: socColor }}>
              {isCharging ? 'CHARGING' : currentDirection.toUpperCase()}
            </div>
          </div>

          {renderSOCGauge(status.battery_soc)}

          <div className="grid grid-cols-3 gap-3 mt-6 text-center">
            <div>
              <div className="text-xs text-[var(--sp-text-subtle)]">VOLTAGE</div>
              <div className="metric-value text-2xl font-semibold tabular-nums mt-0.5">
                {status.battery_voltage.toFixed(1)}
                <span className="text-xs font-normal ml-0.5 text-[var(--sp-text-subtle)]">V</span>
              </div>
            </div>
            <div>
              <div className="text-xs text-[var(--sp-text-subtle)]">CURRENT</div>
              <div className="metric-value text-2xl font-semibold tabular-nums mt-0.5" 
                   style={{ color: status.battery_current < 0 ? 'var(--sp-success)' : undefined }}>
                {status.battery_current > 0 ? '+' : ''}{status.battery_current.toFixed(1)}
                <span className="text-xs font-normal ml-0.5 text-[var(--sp-text-subtle)]">A</span>
              </div>
            </div>
            <div>
              <div className="text-xs text-[var(--sp-text-subtle)]">TEMP</div>
              <div className="metric-value text-2xl font-semibold tabular-nums mt-0.5">
                {status.battery_temp.toFixed(1)}
                <span className="text-xs font-normal ml-0.5 text-[var(--sp-text-subtle)]">°C</span>
              </div>
            </div>
          </div>
        </div>

        {/* INVERTER / POWER SECTION */}
        <div className="card">
          <div className="flex items-center gap-2 mb-3">
            <Zap className="w-5 h-5 text-[var(--sp-accent)]" />
            <div className="font-semibold tracking-tight">Inverter Output</div>
          </div>

          <div className="flex items-baseline gap-1 mb-1">
            <div className="power-value text-[var(--sp-text)]">{status.inverter_power}</div>
            <div className="text-xl font-medium text-[var(--sp-text-muted)]">W</div>
          </div>

          <div className="text-sm text-[var(--sp-text-muted)] mb-4">
            {status.inverter_voltage.toFixed(0)} V • {status.inverter_frequency.toFixed(1)} Hz
          </div>

          {/* Load percentage bar */}
          <div>
            <div className="flex justify-between text-xs mb-1.5 text-[var(--sp-text-muted)]">
              <div>LOAD</div>
              <div className="tabular-nums font-medium">{status.inverter_load_percent.toFixed(0)}%</div>
            </div>
            <div className="h-3 bg-[var(--sp-border)] rounded-full overflow-hidden">
              <motion.div
                className="h-full rounded-full"
                style={{ background: status.inverter_load_percent > 85 ? 'var(--sp-danger)' : 'var(--sp-primary)' }}
                initial={{ width: 0 }}
                animate={{ width: `${Math.min(100, status.inverter_load_percent)}%` }}
                transition={{ duration: 0.5 }}
              />
            </div>
          </div>
        </div>

        {/* OUTLETS CONTROL - the most important field feature */}
        <div className="card">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <Power className="w-5 h-5 text-[var(--sp-accent)]" />
              <div className="font-semibold tracking-tight">AC Outlets</div>
            </div>
            <div className="text-xs text-[var(--sp-text-subtle)]">6 channels • relay controlled</div>
          </div>

          <div className="divide-y divide-[var(--sp-border)]">
            {DEFAULT_OUTLETS.map((outlet) => {
              const isOn = status.outlets[outlet.id] ?? false;
              return (
                <div key={outlet.id} className="outlet-row">
                  <div className="flex items-center gap-3">
                    <div className={`w-9 h-9 rounded-xl flex items-center justify-center ${isOn ? 'bg-[var(--sp-primary)]/10' : 'bg-[var(--sp-border)]'}`}>
                      <Power className={`w-4 h-4 ${isOn ? 'text-[var(--sp-primary)]' : 'text-[var(--sp-text-subtle)]'}`} />
                    </div>
                    <div>
                      <div className="font-medium">{outlet.name}</div>
                      <div className="text-[10px] text-[var(--sp-text-subtle)]">Channel {outlet.id}</div>
                    </div>
                  </div>

                  <button
                    onClick={() => toggleOutlet(outlet.id, !isOn)}
                    disabled={isLoading}
                    className={`outlet-toggle ${isOn ? 'active' : ''}`}
                    aria-label={`Toggle ${outlet.name}`}
                  />
                </div>
              );
            })}
          </div>

          <div className="mt-4 text-[10px] text-center text-[var(--sp-text-subtle)]">
            Toggles send real-time commands to the Raspberry Pi GPIO relays
          </div>
        </div>

        {/* SYSTEM FOOTER */}
        <div className="flex items-center justify-between text-xs px-1 text-[var(--sp-text-subtle)]">
          <div className="flex items-center gap-1.5">
            <Clock className="w-3.5 h-3.5" />
            <span>
              Updated {lastSync ? formatDistanceToNow(lastSync, { addSuffix: true }) : 'just now'}
            </span>
          </div>
          <div className="font-mono text-[10px]">{status.system_mode.replace('_', ' ')}</div>
        </div>

        {/* Footer info */}
        <div className="text-center pt-4">
          <button 
            onClick={() => {
              localStorage.removeItem('sitepulse_last_url');
              setIsConnected(false);
              setStatus(null);
            }}
            className="text-xs text-[var(--sp-text-subtle)] hover:text-[var(--sp-text-muted)] underline-offset-2 hover:underline"
          >
            Disconnect &amp; change controller
          </button>
        </div>
      </div>
    </div>
  );
}

export default App;
