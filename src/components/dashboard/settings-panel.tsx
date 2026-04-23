"use client";

import { useState, useEffect, useCallback } from "react";
import { motion } from "framer-motion";
import {
  Settings,
  Key,
  TrendingUp,
  Shield,
  Zap,
  Eye,
  Save,
  RotateCcw,
  Loader2,
  UserCircle,
  History,
  Trash2,
  Download,
  Upload,
  Plus,
  Check,
  AlertTriangle,
  CircleHelp,
  Globe,
  ShieldCheck,
  Database,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/hooks/use-toast";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

const fadeUp = {
  hidden: { opacity: 0, y: 12 },
  show: (i: number) => ({
    opacity: 1,
    y: 0,
    transition: { delay: i * 0.04, duration: 0.35, ease: "easeOut" as const },
  }),
};

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

interface SettingField {
  key: string;
  label: string;
  type: "text" | "password" | "number" | "select" | "toggle";
  value: string | number | boolean;
  defaultValue: string | number | boolean;
  placeholder?: string;
  options?: Array<{ label: string; value: string }>;
  hint?: string;
}

function HelpIcon({ text }: { text: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button type="button" className="inline-flex items-center justify-center">
          <CircleHelp className="h-3.5 w-3.5 text-gray-500 hover:text-cyan-300 transition-colors" />
        </button>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-[260px] bg-neutral-900 text-neutral-100 border border-white/10">
        {text}
      </TooltipContent>
    </Tooltip>
  );
}

interface SettingSection {
  id: string;
  title: string;
  icon: React.ReactNode;
  fields: SettingField[];
}

interface DBSetting {
  id: string;
  key: string;
  value: string;
  section: string;
  description?: string | null;
  createdAt: string;
  updatedAt: string;
}

interface Profile {
  id: number;
  name: string;
  description: string;
  settings: string;
  isActive: boolean;
  createdAt: string;
  updatedAt: string;
}

interface AuditEntry {
  id: number;
  section: string;
  key: string;
  oldValue: string | null;
  newValue: string | null;
  source: string;
  createdAt: string;
}

/* ------------------------------------------------------------------ */
/*  Default settings blueprint                                         */
/* ------------------------------------------------------------------ */

function buildDefaultSections(): SettingSection[] {
  return [
    {
      id: "trading",
      title: "Trading Parameters",
      icon: <TrendingUp className="h-4 w-4 text-emerald-400" />,
      fields: [
        { key: "symbol", label: "Trading Symbol", type: "select", value: "XAU_USD", defaultValue: "XAU_USD", options: [
          { label: "XAU/USD (Gold)", value: "XAU_USD" },
          { label: "XAG/USD (Silver)", value: "XAG_USD" },
          { label: "EUR/USD", value: "EUR_USD" },
          { label: "GBP/USD", value: "GBP_USD" },
          { label: "USD/JPY", value: "USD_JPY" },
          { label: "WTI/USD (Oil)", value: "WTI_USD" },
          { label: "US30 (Dow Jones)", value: "US30_USD" },
          { label: "NAS100 (NASDAQ)", value: "NAS100_USD" },
        ]},
        { key: "timeframe", label: "Timeframe", type: "select", value: "5m", defaultValue: "5m", options: [{ label: "1m", value: "1m" }, { label: "5m", value: "5m" }, { label: "15m", value: "15m" }, { label: "1h", value: "1h" }] },
        { key: "loop_sleep", label: "Loop Sleep (seconds)", type: "number", value: 5, defaultValue: 5, hint: "Time between trading loop iterations" },
        { key: "history_limit", label: "History Limit", type: "number", value: 200, defaultValue: 200, hint: "Number of candles to fetch" },
      ],
    },
    {
      id: "signals",
      title: "Signal Thresholds",
      icon: <Zap className="h-4 w-4 text-yellow-400" />,
      fields: [
        { key: "min_confidence", label: "Min Confidence", type: "number", value: 0.62, defaultValue: 0.62, hint: "Minimum signal confidence to trade (0-1)" },
        { key: "strong_confidence", label: "Strong Confidence", type: "number", value: 0.78, defaultValue: 0.78, hint: "Threshold for strong signals" },
        { key: "adx_min", label: "ADX Minimum", type: "number", value: 18, defaultValue: 18, hint: "Minimum ADX for trend confirmation" },
        { key: "max_spread", label: "Max Spread (USDT)", type: "number", value: 1.5, defaultValue: 1.5, hint: "Maximum allowed spread" },
        { key: "min_volume", label: "Min Volume Ratio", type: "number", value: 0.5, defaultValue: 0.5, hint: "Minimum volume ratio to trade" },
      ],
    },
    {
      id: "risk",
      title: "Risk Management",
      icon: <Shield className="h-4 w-4 text-red-400" />,
      fields: [
        { key: "risk_per_trade", label: "Risk Per Trade (%)", type: "number", value: 1.2, defaultValue: 1.2, hint: "Percentage of capital risked per trade" },
        { key: "max_trades_day", label: "Max Trades / Day", type: "number", value: 120, defaultValue: 120, hint: "Maximum trades per day" },
        { key: "daily_loss_limit", label: "Daily Loss Limit ($)", type: "number", value: 50, defaultValue: 50, hint: "Stop trading after this loss" },
        { key: "max_drawdown", label: "Max Drawdown (%)", type: "number", value: 15, defaultValue: 15, hint: "Maximum allowed drawdown" },
        { key: "stop_loss", label: "Default Stop Loss (%)", type: "number", value: 1.5, defaultValue: 1.5, hint: "Default stop loss percentage" },
        { key: "take_profit", label: "Default Take Profit (%)", type: "number", value: 2.5, defaultValue: 2.5, hint: "Default take profit percentage" },
      ],
    },
    {
      id: "execution",
      title: "Execution Engine",
      icon: <Zap className="h-4 w-4 text-purple-400" />,
      fields: [
        { key: "max_slippage", label: "Max Slippage (%)", type: "number", value: 0.1, defaultValue: 0.1, hint: "Maximum allowed slippage" },
        { key: "max_spread_exec", label: "Max Spread Exec (USDT)", type: "number", value: 2.0, defaultValue: 2.0, hint: "Max spread during execution" },
        { key: "order_timeout", label: "Order Timeout (ms)", type: "number", value: 5000, defaultValue: 5000, hint: "Order timeout in milliseconds" },
        { key: "split_threshold", label: "Split Threshold ($)", type: "number", value: 500, defaultValue: 500, hint: "Split orders above this size" },
        { key: "retry_attempts", label: "Retry Attempts", type: "number", value: 3, defaultValue: 3, hint: "Number of retry attempts" },
      ],
    },
    {
      id: "autopause",
      title: "Auto-Pause Controls",
      icon: <Shield className="h-4 w-4 text-orange-400" />,
      fields: [
        { key: "pause_on_loss_limit", label: "Pause on Daily Loss Limit", type: "toggle", value: true, defaultValue: true },
        { key: "pause_on_drawdown", label: "Pause on Max Drawdown", type: "toggle", value: true, defaultValue: true },
        { key: "pause_on_spread", label: "Pause on High Spread", type: "toggle", value: true, defaultValue: true },
        { key: "pause_on_disconnect", label: "Pause on Disconnect", type: "toggle", value: true, defaultValue: true },
      ],
    },
    {
      id: "display",
      title: "Display Options",
      icon: <Eye className="h-4 w-4 text-cyan-400" />,
      fields: [
        { key: "terminal_tui", label: "Terminal TUI", type: "toggle", value: false, defaultValue: false, hint: "Show terminal UI on bot server" },
        { key: "low_ram", label: "Low RAM Mode", type: "toggle", value: false, defaultValue: false, hint: "Reduce memory usage" },
      ],
    },
  ];
}

/* ------------------------------------------------------------------ */
/*  Helper: merge DB settings into default sections                    */
/* ------------------------------------------------------------------ */

function mergeSettings(
  defaults: SettingSection[],
  dbSettings: DBSetting[]
): SettingSection[] {
  const lookup = new Map<string, string>();
  dbSettings.forEach((s) => lookup.set(s.key, s.value));

  return defaults.map((sec) => ({
    ...sec,
    fields: sec.fields.map((f) => {
      const raw = lookup.get(f.key);
      if (raw === undefined) return f;
      let parsed: string | number | boolean = raw;
      if (f.type === "number") parsed = parseFloat(raw) || 0;
      else if (f.type === "toggle") parsed = raw === "true";
      return { ...f, value: parsed };
    }),
  }));
}

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

export function SettingsPanel() {
  const [sections, setSections] = useState<SettingSection[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState(false);

  // Account mode state
  const [accountMode, setAccountModeState] = useState<"testnet" | "real">("testnet");
  const [modeLoading, setModeLoading] = useState(false);
  const [showRealConfirm, setShowRealConfirm] = useState(false);

  // Broker credential state
  const [selectedBroker, setSelectedBroker] = useState<"oanda" | "weltrade_mt5" | "ctrader">("weltrade_mt5");
  const [brokerAccountId, setBrokerAccountId] = useState("");
  const [brokerApiToken, setBrokerApiToken] = useState("");
  const [brokerServer, setBrokerServer] = useState("");
  const [brokerIsDemo, setBrokerIsDemo] = useState(true);
  const [credStatus, setCredStatus] = useState<{ configured: boolean; accountIdPrefix: string | null; isDemo: boolean; broker?: string } | null>(null);
  const [validating, setValidating] = useState(false);
  const [savingCreds, setSavingCreds] = useState(false);

  // Profiles
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [profileName, setProfileName] = useState("");
  const [profileDesc, setProfileDesc] = useState("");
  const [profilesLoading, setProfilesLoading] = useState(false);

  // Audit log
  const [auditLog, setAuditLog] = useState<AuditEntry[]>([]);
  const [auditLoading, setAuditLoading] = useState(false);

  /* ---- Load account mode and credential status ---- */
  const loadAccountMode = useCallback(async () => {
    try {
      const res = await fetch("/api/config/mode");
      if (res.ok) {
        const data = await res.json();
        setAccountModeState(data.testnet ? "testnet" : "real");
        if (data.broker) {
          setSelectedBroker(data.broker);
        }
        if (data.credentialsStatus) {
          setCredStatus((prev) => ({
            configured: !!data.credentialsStatus.configured,
            accountIdPrefix: prev?.accountIdPrefix || null,
            isDemo: typeof data.testnet === "boolean" ? data.testnet : (prev?.isDemo ?? true),
            broker: data.broker || prev?.broker,
          }));
        }
      }
    } catch { /* silent */ }
  }, []);

  /* ---- Load credential status ---- */
  const loadCredentialStatus = useCallback(async () => {
    try {
      const res = await fetch(`/api/config/credentials?broker=${selectedBroker}`);
      if (res.ok) {
        const data = await res.json();
        setCredStatus({
          configured: data.configured,
          accountIdPrefix: data.accountIdPrefix,
          isDemo: data.isDemo,
          broker: data.broker,
        });
        if (typeof data.isDemo === "boolean") {
          setBrokerIsDemo(data.isDemo);
        }
      }
    } catch { /* silent */ }
  }, [selectedBroker]);

  /* ---- Switch account mode ---- */
  const handleSwitchMode = async (toTestnet: boolean) => {
    // If switching to real, require confirmation
    if (!toTestnet && !showRealConfirm) {
      setShowRealConfirm(true);
      return;
    }

    setShowRealConfirm(false);
    setModeLoading(true);
    try {
      const res = await fetch("/api/config/mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ testnet: toTestnet }),
      });
      const data = await res.json();
      if (res.ok && data.success) {
        setAccountModeState(toTestnet ? "testnet" : "real");
        toast({
          title: toTestnet ? "Modo Testnet Activado" : "Modo Cuenta Real Activado",
          description: data.message,
          variant: toTestnet ? "default" : "destructive",
        });
        loadCredentialStatus();
      } else {
        const errorMsg = data.error || data.requiresCredentials
          ? "No hay credenciales configuradas para este modo. Agrega las API keys abajo."
          : "No se pudo cambiar el modo";
        toast({ title: "Error al cambiar modo", description: errorMsg, variant: "destructive" });
      }
    } catch {
      toast({ title: "Error", description: "Error de conexión", variant: "destructive" });
    } finally {
      setModeLoading(false);
    }
  };

  /* ---- Save broker credentials ---- */
  const handleSaveBrokerCredentials = async () => {
    if (!brokerAccountId.trim() || !brokerApiToken.trim()) {
      toast({
        title: "Campos requeridos",
        description: selectedBroker === "weltrade_mt5"
          ? "MT5 Login y MT5 Password son obligatorios"
          : "Account ID y API Token son obligatorios",
        variant: "destructive",
      });
      return;
    }
    setSavingCreds(true);
    try {
      const res = await fetch(`/api/config/credentials?broker=${selectedBroker}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          broker: selectedBroker,
          accountId: brokerAccountId.trim(),
          apiToken: brokerApiToken.trim(),
          isDemo: brokerIsDemo,
          extra: selectedBroker === "weltrade_mt5" ? {
            server: brokerServer.trim(),
            demoServer: "MetaQuotes-Demo",
            liveServer: "Weltrade-Live",
          } : undefined,
          makeActive: true,
        }),
      });
      if (res.ok) {
        toast({ title: "Credenciales guardadas", description: "Conexión configurada exitosamente" });
        loadCredentialStatus();
        setBrokerAccountId("");
        setBrokerApiToken("");
      } else {
        toast({ title: "Error", description: "No se pudieron guardar las credenciales", variant: "destructive" });
      }
    } catch {
      toast({ title: "Error", description: "Error de conexión", variant: "destructive" });
    } finally {
      setSavingCreds(false);
    }
  };

  /* ---- Validate broker credentials ---- */
  const handleValidateBrokerCredentials = async () => {
    setValidating(true);
    try {
      const body: Record<string, any> = { isDemo: brokerIsDemo, broker: selectedBroker };
      if (brokerAccountId.trim()) body.accountId = brokerAccountId.trim();
      if (brokerApiToken.trim()) body.apiToken = brokerApiToken.trim();
      if (selectedBroker === "weltrade_mt5" && brokerServer.trim()) {
        body.extra = {
          server: brokerServer.trim(),
          demoServer: "MetaQuotes-Demo",
          liveServer: "Weltrade-Live",
        };
      }
      const res = await fetch(`/api/config/credentials?broker=${selectedBroker}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (data.success) {
        toast({
          title: "Conexión exitosa",
          description: data.message + (data.balance ? ` | Balance: $${data.balance.toFixed(2)}` : ""),
        });
        loadCredentialStatus();
      } else {
        toast({ title: "Credenciales inválidas", description: data.message || "Verifica tus credenciales", variant: "destructive" });
      }
    } catch {
      toast({ title: "Error", description: "Error de validación", variant: "destructive" });
    } finally {
      setValidating(false);
    }
  };

  /* ---- Load all settings ---- */
  const loadSettings = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/config/settings");
      if (!res.ok) throw new Error("Failed to fetch settings");
      const { settings } = await res.json();
      const defaults = buildDefaultSections();
      setSections(mergeSettings(defaults, settings));
    } catch (err) {
      console.error(err);
      toast({
        title: "Load Error",
        description: "Could not load settings. Using defaults.",
        variant: "destructive",
      });
      setSections(buildDefaultSections());
    } finally {
      setLoading(false);
    }
  }, []);

  /* ---- Load profiles ---- */
  const loadProfiles = useCallback(async () => {
    setProfilesLoading(true);
    try {
      const res = await fetch("/api/profiles");
      if (res.ok) {
        const data = await res.json();
        setProfiles(data.profiles || []);
      }
    } catch {
      /* silent */
    } finally {
      setProfilesLoading(false);
    }
  }, []);

  /* ---- Load audit log ---- */
  const loadAuditLog = useCallback(async () => {
    setAuditLoading(true);
    try {
      const res = await fetch("/api/audit-log?limit=10");
      if (res.ok) {
        const data = await res.json();
        setAuditLog(data.changes || []);
      }
    } catch {
      /* silent */
    } finally {
      setAuditLoading(false);
    }
  }, []);

  useEffect(() => {
    loadAccountMode();
    loadCredentialStatus();
    loadSettings();
    loadProfiles();
    loadAuditLog();
  }, [loadAccountMode, loadCredentialStatus, loadSettings, loadProfiles, loadAuditLog]);

  /* ---- Update a single field ---- */
  const updateField = useCallback(
    (sectionId: string, fieldKey: string, newValue: string | number | boolean) => {
      setSections((prev) =>
        prev.map((s) =>
          s.id === sectionId
            ? {
                ...s,
                fields: s.fields.map((f) =>
                  f.key === fieldKey ? { ...f, value: newValue } : f
                ),
              }
            : s
        )
      );
    },
    []
  );

  /* ---- Collect changed settings ---- */
  const getChangedSettings = useCallback(() => {
    const defaults = buildDefaultSections();
    const defaultMap = new Map<string, { value: string; section: string }>();
    defaults.forEach((sec) =>
      sec.fields.forEach((f) =>
        defaultMap.set(f.key, {
          value: String(f.defaultValue),
          section: sec.id,
        })
      )
    );

    const updates: { key: string; value: string; section: string }[] = [];
    sections.forEach((sec) =>
      sec.fields.forEach((f) => {
        const current = String(f.value);
        const def = defaultMap.get(f.key);
        // Include if different from default (or if no default exists in DB yet)
        if (!def || def.value !== current) {
          updates.push({ key: f.key, value: current, section: sec.id });
        }
      })
    );
    return updates;
  }, [sections]);

  /* ---- Save All ---- */
  const handleSave = async () => {
    setSaving(true);
    try {
      const updates = getChangedSettings();
      const res = await fetch("/api/config/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings: updates }),
      });
      if (!res.ok) throw new Error("Save failed");
      toast({
        title: "Settings Saved",
        description: `${updates.length} setting(s) updated successfully.`,
      });
      loadAuditLog(); // refresh audit log after save
    } catch (err) {
      console.error(err);
      toast({
        title: "Save Error",
        description: "Could not save settings. Please try again.",
        variant: "destructive",
      });
    } finally {
      setSaving(false);
    }
  };

  /* ---- Reset ---- */
  const handleReset = async () => {
    setResetting(true);
    try {
      const res = await fetch("/api/config/settings/reset", { method: "POST" });
      if (!res.ok) throw new Error("Reset failed");
      toast({ title: "Settings Reset", description: "All settings restored to defaults." });
      loadSettings();
      loadAuditLog();
    } catch (err) {
      console.error(err);
      toast({
        title: "Reset Error",
        description: "Could not reset settings.",
        variant: "destructive",
      });
    } finally {
      setResetting(false);
    }
  };

  /* ---- Save as Profile ---- */
  const handleSaveProfile = async () => {
    const name = profileName.trim();
    if (!name) {
      toast({ title: "Profile Name Required", description: "Enter a name for the profile.", variant: "destructive" });
      return;
    }
    try {
      // Gather all current settings
      const allSettings: { key: string; value: string; section: string }[] = [];
      sections.forEach((sec) =>
        sec.fields.forEach((f) =>
          allSettings.push({ key: f.key, value: String(f.value), section: sec.id })
        )
      );

      const res = await fetch("/api/profiles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "create",
          name,
          description: profileDesc.trim() || undefined,
          settings: allSettings,
        }),
      });
      if (!res.ok) throw new Error("Create profile failed");
      toast({ title: "Profile Created", description: `"${name}" saved as a config profile.` });
      setProfileName("");
      setProfileDesc("");
      loadProfiles();
    } catch {
      toast({ title: "Profile Error", description: "Could not create profile.", variant: "destructive" });
    }
  };

  /* ---- Load Profile ---- */
  const handleLoadProfile = async (profile: Profile) => {
    try {
      const res = await fetch("/api/profiles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "load", id: profile.id }),
      });
      if (!res.ok) throw new Error("Load profile failed");
      toast({ title: "Profile Loaded", description: `"${profile.name}" applied.` });
      loadSettings();
      loadProfiles();
      loadAuditLog();
    } catch {
      toast({ title: "Load Error", description: "Could not load profile.", variant: "destructive" });
    }
  };

  /* ---- Delete Profile ---- */
  const handleDeleteProfile = async (profile: Profile) => {
    try {
      const res = await fetch("/api/profiles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "delete", id: profile.id }),
      });
      if (!res.ok) throw new Error("Delete profile failed");
      toast({ title: "Profile Deleted", description: `"${profile.name}" removed.` });
      loadProfiles();
    } catch {
      toast({ title: "Delete Error", description: "Could not delete profile.", variant: "destructive" });
    }
  };

  /* ---- Format timestamp ---- */
  const formatTime = (iso: string) => {
    try {
      const d = new Date(iso);
      return d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
    } catch {
      return iso;
    }
  };

  /* ---- Mask sensitive values ---- */
  const maskValue = (key: string, val: string) => {
    if (key === "api_key" || key === "api_secret") {
      if (!val) return "â€¢â€¢â€¢â€¢â€¢â€¢â€¢â€¢";
      return val.length > 8 ? val.slice(0, 4) + "â€¢â€¢â€¢â€¢" + val.slice(-4) : "â€¢â€¢â€¢â€¢â€¢â€¢â€¢â€¢";
    }
    return val || "-";
  };

  /* ================================================================== */
  /*  RENDER                                                             */
  /* ================================================================== */

  return (
    <div className="space-y-4 p-4 lg:p-6 max-h-[calc(100vh-3.5rem)] overflow-y-auto custom-scrollbar">
      {/* ---- Header ---- */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-bold text-white flex items-center gap-2">
            <Settings className="h-5 w-5 text-gray-400" />
            Bot Settings
          </h2>
          <p className="text-xs text-gray-500 mt-0.5">
            Configure trading bot parameters â€” changes are persisted to database
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="ghost"
            size="sm"
            className="text-gray-400 hover:text-white"
            onClick={handleReset}
            disabled={resetting || loading}
          >
            {resetting ? <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" /> : <RotateCcw className="h-3.5 w-3.5 mr-1.5" />}
            Reset
          </Button>
          <Button
            size="sm"
            className="bg-blue-600 hover:bg-blue-700 text-white"
            onClick={handleSave}
            disabled={saving || loading}
          >
            {saving ? <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" /> : <Save className="h-3.5 w-3.5 mr-1.5" />}
            Save All
          </Button>
        </div>
      </div>

      {/* ---- Account Mode Selector ---- */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        className="rounded-xl p-5 border"
        style={{
          background: accountMode === "testnet"
            ? "linear-gradient(135deg, rgba(16, 185, 129, 0.08), rgba(16, 185, 129, 0.02))"
            : "linear-gradient(135deg, rgba(239, 68, 68, 0.08), rgba(239, 68, 68, 0.02))",
          borderColor: accountMode === "testnet"
            ? "rgba(16, 185, 129, 0.25)"
            : "rgba(239, 68, 68, 0.25)",
        }}
      >
        <div className="flex items-center gap-2 mb-3">
          <Globe className="h-4 w-4" style={{ color: accountMode === "testnet" ? "#10b981" : "#ef4444" }} />
          <h3 className="text-sm font-semibold text-white">Modo de Cuenta</h3>
          {modeLoading && <Loader2 className="h-3.5 w-3.5 animate-spin text-gray-400 ml-1" />}
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {/* Testnet Option */}
          <button
            onClick={() => handleSwitchMode(true)}
            disabled={modeLoading || accountMode === "testnet"}
            className={cn(
              "relative flex flex-col items-center gap-2 p-4 rounded-xl border-2 transition-all duration-300",
              accountMode === "testnet"
                ? "border-emerald-500 bg-emerald-500/10 shadow-lg shadow-emerald-500/10"
                : "border-white/[0.08] bg-white/[0.02] hover:border-emerald-500/40 hover:bg-emerald-500/5",
              modeLoading && "opacity-50 cursor-not-allowed"
            )}
          >
            <div className={cn(
              "w-10 h-10 rounded-full flex items-center justify-center",
              accountMode === "testnet" ? "bg-emerald-500/20" : "bg-white/[0.06]"
            )}>
              <ShieldCheck className={cn("h-5 w-5", accountMode === "testnet" ? "text-emerald-400" : "text-gray-500")} />
            </div>
            <div className="text-center">
              <p className={cn("text-sm font-semibold", accountMode === "testnet" ? "text-emerald-400" : "text-gray-300")}>
                Testnet
              </p>
              <p className="text-[10px] text-gray-500 mt-0.5">Capital ficticio</p>
            </div>
            {accountMode === "testnet" && (
              <div className="absolute top-2 right-2">
                <Check className="h-4 w-4 text-emerald-400" />
              </div>
            )}
          </button>

          {/* Real Account Option */}
          <button
            onClick={() => handleSwitchMode(false)}
            disabled={modeLoading || accountMode === "real"}
            className={cn(
              "relative flex flex-col items-center gap-2 p-4 rounded-xl border-2 transition-all duration-300",
              accountMode === "real"
                ? "border-red-500 bg-red-500/10 shadow-lg shadow-red-500/10"
                : "border-white/[0.08] bg-white/[0.02] hover:border-red-500/40 hover:bg-red-500/5",
              modeLoading && "opacity-50 cursor-not-allowed"
            )}
          >
            <div className={cn(
              "w-10 h-10 rounded-full flex items-center justify-center",
              accountMode === "real" ? "bg-red-500/20" : "bg-white/[0.06]"
            )}>
              <Database className={cn("h-5 w-5", accountMode === "real" ? "text-red-400" : "text-gray-500")} />
            </div>
            <div className="text-center">
              <p className={cn("text-sm font-semibold", accountMode === "real" ? "text-red-400" : "text-gray-300")}>
                Cuenta Real
              </p>
              <p className="text-[10px] text-gray-500 mt-0.5">Dinero real</p>
            </div>
            {accountMode === "real" && (
              <div className="absolute top-2 right-2">
                <Check className="h-4 w-4 text-red-400" />
              </div>
            )}
          </button>
        </div>

        {/* Status bar */}
        <div className="mt-3 flex items-center gap-2 rounded-lg px-3 py-2" style={{
          background: accountMode === "testnet"
            ? "rgba(16, 185, 129, 0.06)"
            : "rgba(239, 68, 68, 0.06)",
        }}>
          <div className={cn("w-2 h-2 rounded-full", accountMode === "testnet" ? "bg-emerald-400" : "bg-red-400")} />
          <span className={cn("text-xs font-mono", accountMode === "testnet" ? "text-emerald-400" : "text-red-400")}>
            {accountMode === "testnet"
              ? `${selectedBroker} demo - Operaciones con dinero ficticio, sin riesgo`
              : `${selectedBroker} live - DINERO REAL, riesgo financiero`}
          </span>
        </div>

        {/* Warning when switching to real */}
        {showRealConfirm && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            className="mt-3 p-3 rounded-lg border border-red-500/30 bg-red-500/10"
          >
            <div className="flex items-start gap-2">
              <AlertTriangle className="h-4 w-4 text-red-400 mt-0.5 shrink-0" />
              <div>
                <p className="text-xs font-semibold text-red-400">Atención: Cuenta Real</p>
                <p className="text-[10px] text-red-300/70 mt-1">
                  Al activar la cuenta real, todas las operaciones usarán dinero real de tu broker activo.
                  Asegúrate de tener configuradas las API keys correctas. ¿Continuar?
                </p>
                <div className="flex gap-2 mt-2">
                  <button
                    onClick={() => setShowRealConfirm(false)}
                    className="px-3 py-1 text-[10px] rounded-md bg-white/[0.06] text-gray-300 hover:bg-white/[0.1] border border-white/[0.08]"
                  >
                    Cancelar
                  </button>
                  <button
                    onClick={() => handleSwitchMode(false)}
                    className="px-3 py-1 text-[10px] rounded-md bg-red-500/20 text-red-400 hover:bg-red-500/30 border border-red-500/30 font-semibold"
                  >
                    Sí, activar cuenta real
                  </button>
                </div>
              </div>
            </div>
          </motion.div>
        )}
      </motion.div>

      {/* ---- Broker Credentials ---- */}
      <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} className="glass-card rounded-xl p-5">
        <div className="flex items-center gap-2 mb-4">
          <Key className="h-4 w-4 text-blue-400" />
          <h3 className="text-sm font-semibold text-white">Broker Credentials</h3>
          <p className="text-[10px] text-gray-500 ml-1">Configura Weltrade MT5 desde aquí para operación automática</p>
        </div>
        <div className="rounded-lg border border-white/[0.06] bg-white/[0.02] p-4 space-y-3">
          <div className="space-y-1">
            <div className="flex items-center gap-1.5">
              <Label className="text-xs text-gray-400">Broker activo</Label>
              <HelpIcon text="Selecciona el broker de ejecución principal del bot." />
            </div>
            <Select value={selectedBroker} onValueChange={(v) => setSelectedBroker(v as any)}>
              <SelectTrigger className="bg-white/[0.04] border-white/[0.08] text-xs h-8">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="weltrade_mt5">Weltrade MT5</SelectItem>
                <SelectItem value="oanda">OANDA (backup)</SelectItem>
                <SelectItem value="ctrader">cTrader</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {selectedBroker === "weltrade_mt5" && (
            <div className="rounded-lg border border-emerald-500/25 bg-emerald-500/10 px-3 py-2">
              <p className="text-[10px] text-emerald-300">
                Flujo recomendado: en Demo usa servidor MetaQuotes-Demo; en Real usa servidor Weltrade-Live (o el exacto entregado por tu broker).
                Luego pulsa Validar y Guardar.
              </p>
            </div>
          )}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-1.5">
              <Label className="text-xs text-gray-400">Cuenta Demo</Label>
              <HelpIcon text="Activa demo para pruebas sin riesgo. Desactiva para operar en cuenta real." />
            </div>
            <Switch checked={brokerIsDemo} onCheckedChange={setBrokerIsDemo} />
          </div>
          <p className="text-[10px] text-gray-500 -mt-2">
            {brokerIsDemo ? "Demo - Dinero ficticio" : "Live - DINERO REAL"}
          </p>
          <div className="space-y-2">
            <Input type="text" placeholder={selectedBroker === "weltrade_mt5" ? "MT5 Login" : "Account ID"} value={brokerAccountId} onChange={(e) => setBrokerAccountId(e.target.value)} className="bg-white/[0.04] border-white/[0.08] text-xs h-8 font-mono" />
            <Input type="password" placeholder={selectedBroker === "weltrade_mt5" ? "MT5 Password" : "API Token"} value={brokerApiToken} onChange={(e) => setBrokerApiToken(e.target.value)} className="bg-white/[0.04] border-white/[0.08] text-xs h-8 font-mono" />
            {selectedBroker === "weltrade_mt5" && (
              <Input type="text" placeholder={brokerIsDemo ? "MetaQuotes-Demo (recomendado para pruebas)" : "Weltrade-Live (cuenta real)"} value={brokerServer} onChange={(e) => setBrokerServer(e.target.value)} className="bg-white/[0.04] border-white/[0.08] text-xs h-8 font-mono" />
            )}
          </div>
          <div className="flex gap-2">
            <button onClick={handleSaveBrokerCredentials} disabled={savingCreds} className="px-2.5 py-1 text-[10px] rounded-md bg-blue-500/15 text-blue-400 hover:bg-blue-500/25 border border-blue-500/30 disabled:opacity-40 transition-all">
              {savingCreds ? <Loader2 className="h-3 w-3 animate-spin" /> : "Guardar"}
            </button>
            <button onClick={handleValidateBrokerCredentials} disabled={validating} className="px-2.5 py-1 text-[10px] rounded-md bg-white/[0.06] text-gray-400 hover:text-gray-200 border border-white/[0.08] disabled:opacity-40 transition-all">
              {validating ? <Loader2 className="h-3 w-3 animate-spin" /> : "Validar"}
            </button>
          </div>
          {credStatus?.configured && <p className="text-[10px] text-emerald-400 mt-2">Conectado ({credStatus.broker || selectedBroker}): {credStatus.accountIdPrefix}...</p>}
        </div>
      </motion.div>

      {/* ---- Loading skeleton ---- */}
      {loading ? (
        <div className="space-y-4">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="glass-card rounded-xl p-5 space-y-4">
              <Skeleton className="h-5 w-40 bg-white/10" />
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {Array.from({ length: 4 }).map((_, j) => (
                  <div key={j} className="space-y-2">
                    <Skeleton className="h-3 w-28 bg-white/10" />
                    <Skeleton className="h-9 w-full bg-white/5" />
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="space-y-4">
          {/* ---- 7 Setting Sections ---- */}
          {sections.map((section, si) => (
            <motion.div
              key={section.id}
              variants={fadeUp}
              initial="hidden"
              animate="show"
              custom={si}
              className="glass-card rounded-xl p-5"
            >
              <div className="flex items-center gap-2 mb-4">
                {section.icon}
                <h3 className="text-sm font-semibold text-white">{section.title}</h3>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {section.fields.map((field) => (
                  <div key={field.key} className="space-y-1.5">
                    <div className="flex items-center gap-1.5">
                      <Label className="text-xs text-gray-400">{field.label}</Label>
                      <HelpIcon text={field.hint || `Configura ${field.label} para ajustar el comportamiento del bot.`} />
                    </div>
                    {field.type === "toggle" ? (
                      <div className="flex items-center gap-2">
                        <Switch
                          checked={field.value as boolean}
                          onCheckedChange={(checked) => updateField(section.id, field.key, checked)}
                        />
                        {field.hint && (
                          <span className="text-[10px] text-gray-600">{field.hint}</span>
                        )}
                      </div>
                    ) : field.type === "select" ? (
                      <Select
                        value={field.value as string}
                        onValueChange={(v) => updateField(section.id, field.key, v)}
                      >
                        <SelectTrigger className="bg-white/[0.04] border-white/[0.08] text-sm h-9">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {field.options?.map((opt) => (
                            <SelectItem key={opt.value} value={opt.value}>
                              {opt.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    ) : (
                      <>
                        <Input
                          type={field.type}
                          value={field.value as string | number}
                          onChange={(e) => {
                            const v =
                              field.type === "number"
                                ? parseFloat(e.target.value) || 0
                                : e.target.value;
                            updateField(section.id, field.key, v);
                          }}
                          placeholder={field.placeholder}
                          className="bg-white/[0.04] border-white/[0.08] text-sm h-9 font-mono"
                        />
                        {field.hint && (
                          <p className="text-[10px] text-gray-600">{field.hint}</p>
                        )}
                      </>
                    )}
                  </div>
                ))}
              </div>
            </motion.div>
          ))}

          {/* ---- Config Profiles Section ---- */}
          <motion.div
            variants={fadeUp}
            initial="hidden"
            animate="show"
            custom={8}
            className="glass-card rounded-xl p-5"
          >
            <div className="flex items-center gap-2 mb-4">
              <UserCircle className="h-4 w-4 text-violet-400" />
              <h3 className="text-sm font-semibold text-white">Config Profiles</h3>
            </div>

            {/* Create new profile */}
            <div className="flex flex-col sm:flex-row gap-2 mb-4">
              <Input
                placeholder="Profile name"
                value={profileName}
                onChange={(e) => setProfileName(e.target.value)}
                className="bg-white/[0.04] border-white/[0.08] text-sm h-9 flex-1"
              />
              <Input
                placeholder="Description (optional)"
                value={profileDesc}
                onChange={(e) => setProfileDesc(e.target.value)}
                className="bg-white/[0.04] border-white/[0.08] text-sm h-9 flex-1"
              />
              <Button
                size="sm"
                className="bg-violet-600 hover:bg-violet-700 text-white shrink-0"
                onClick={handleSaveProfile}
              >
                <Plus className="h-3.5 w-3.5 mr-1.5" />
                Save as Profile
              </Button>
            </div>

            {/* Profile list */}
            {profilesLoading ? (
              <div className="space-y-2">
                <Skeleton className="h-10 w-full bg-white/5" />
                <Skeleton className="h-10 w-full bg-white/5" />
              </div>
            ) : profiles.length === 0 ? (
              <p className="text-xs text-gray-500 italic">
                No profiles saved yet. Configure your settings and save a profile.
              </p>
            ) : (
              <div className="space-y-2 max-h-48 overflow-y-auto custom-scrollbar">
                {profiles.map((p) => (
                  <div
                    key={p.id}
                    className="flex items-center justify-between gap-2 rounded-lg bg-white/[0.03] border border-white/[0.06] px-3 py-2"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      {p.isActive && (
                        <Badge
                          variant="outline"
                          className="text-[10px] text-emerald-400 border-emerald-500/40 shrink-0"
                        >
                          Active
                        </Badge>
                      )}
                      <div className="min-w-0">
                        <p className="text-sm text-white truncate">{p.name}</p>
                        {p.description && (
                          <p className="text-[10px] text-gray-500 truncate">{p.description}</p>
                        )}
                      </div>
                    </div>
                    <div className="flex gap-1 shrink-0">
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 px-2 text-blue-400 hover:text-blue-300 hover:bg-blue-500/10"
                        onClick={() => handleLoadProfile(p)}
                        title="Load this profile"
                      >
                        <Upload className="h-3 w-3" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 px-2 text-red-400 hover:text-red-300 hover:bg-red-500/10"
                        onClick={() => handleDeleteProfile(p)}
                        title="Delete this profile"
                      >
                        <Trash2 className="h-3 w-3" />
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </motion.div>

          {/* ---- Recent Changes (Audit Log) Section ---- */}
          <motion.div
            variants={fadeUp}
            initial="hidden"
            animate="show"
            custom={9}
            className="glass-card rounded-xl p-5"
          >
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2">
                <History className="h-4 w-4 text-amber-400" />
                <h3 className="text-sm font-semibold text-white">Recent Changes</h3>
              </div>
              <Button
                variant="ghost"
                size="sm"
                className="h-7 text-gray-500 hover:text-gray-300"
                onClick={loadAuditLog}
              >
                <RotateCcw className="h-3 w-3 mr-1" />
                Refresh
              </Button>
            </div>

            {auditLoading ? (
              <div className="space-y-2">
                {Array.from({ length: 5 }).map((_, i) => (
                  <Skeleton key={i} className="h-8 w-full bg-white/5" />
                ))}
              </div>
            ) : auditLog.length === 0 ? (
              <p className="text-xs text-gray-500 italic">
                No recent changes recorded.
              </p>
            ) : (
              <div className="space-y-1 max-h-72 overflow-y-auto custom-scrollbar">
                {auditLog.map((entry) => (
                  <div
                    key={entry.id}
                    className="flex items-center gap-3 rounded-lg bg-white/[0.02] border border-white/[0.04] px-3 py-2 text-xs"
                  >
                    <span className="text-gray-600 shrink-0 font-mono">
                      {formatTime(entry.createdAt)}
                    </span>
                    <Badge
                      variant="outline"
                      className="text-[10px] text-gray-400 border-gray-700 shrink-0"
                    >
                      {entry.section}
                    </Badge>
                    <span className="text-gray-300 truncate">{entry.key}</span>
                    <span className="text-gray-600 truncate max-w-[100px]">
                      {maskValue(entry.key, entry.oldValue ?? "")}
                    </span>
                    <span className="text-gray-600">&rarr;</span>
                    <span className="text-emerald-400 truncate max-w-[100px]">
                      {maskValue(entry.key, entry.newValue ?? "")}
                    </span>
                    {entry.source && entry.source !== "user" && (
                      <Badge
                        variant="outline"
                        className="text-[10px] text-sky-400 border-sky-700 shrink-0"
                      >
                        {entry.source}
                      </Badge>
                    )}
                  </div>
                ))}
              </div>
            )}
          </motion.div>
        </div>
      )}
    </div>
  );
}




