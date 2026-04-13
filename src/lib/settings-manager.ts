// ============================================
// SettingsManager - Centralized Settings Persistence
// ============================================
// Single source of truth for all application settings.
// Uses the AppSetting table (key-value store with sections).
// Handles: get, set, getAll, getBySection, bulkUpdate, with audit logging.
// ============================================

import { db } from '@/lib/db';

// ---- Types ----

export interface SettingRecord {
  key: string;
  value: string;
  section: string;
  description?: string | null;
}

export interface SettingUpdate {
  key: string;
  value: string;
  section: string;
}

// ---- Default Settings ----
// Matches the settings-panel.tsx sections

const DEFAULT_SETTINGS: SettingUpdate[] = [
  // API Settings
  { key: 'api_key', value: '', section: 'api' },
  { key: 'api_secret', value: '', section: 'api' },
  { key: 'testnet', value: 'true', section: 'api' },
  { key: 'confirm_mainnet', value: 'true', section: 'api' },
  { key: 'broker_active', value: 'weltrade_mt5', section: 'general' },

  // Trading Settings
  { key: 'symbol', value: 'XAU_USD', section: 'trading' },
  { key: 'timeframe', value: '5m', section: 'trading' },
  { key: 'loop_sleep', value: '5', section: 'trading' },
  { key: 'history_limit', value: '500', section: 'trading' },

  // Signal Settings
  { key: 'min_confidence', value: '0.55', section: 'signals' },
  { key: 'strong_confidence', value: '0.75', section: 'signals' },
  { key: 'adx_min', value: '15', section: 'signals' },
  { key: 'max_spread', value: '0.1', section: 'signals' },
  { key: 'min_volume', value: '100', section: 'signals' },

  // Risk Settings
  { key: 'risk_per_trade', value: '1.0', section: 'risk' },
  { key: 'max_trades_day', value: '120', section: 'risk' },
  { key: 'daily_loss_limit', value: '3.0', section: 'risk' },
  { key: 'max_drawdown', value: '10.0', section: 'risk' },
  { key: 'stop_loss', value: '2.0', section: 'risk' },
  { key: 'take_profit', value: '3.0', section: 'risk' },

  // Execution Settings
  { key: 'max_slippage', value: '0.5', section: 'execution' },
  { key: 'max_spread_exec', value: '0.1', section: 'execution' },
  { key: 'order_timeout', value: '30', section: 'execution' },
  { key: 'split_threshold', value: '1000', section: 'execution' },
  { key: 'retry_attempts', value: '3', section: 'execution' },

  // Autopause Settings
  { key: 'pause_on_loss_limit', value: 'true', section: 'autopause' },
  { key: 'pause_on_drawdown', value: 'true', section: 'autopause' },
  { key: 'pause_on_spread', value: 'true', section: 'autopause' },
  { key: 'pause_on_disconnect', value: 'true', section: 'autopause' },

  // Display Settings
  { key: 'terminal_tui', value: 'false', section: 'display' },
  { key: 'low_ram', value: 'false', section: 'display' },
];

// ---- SettingsManager ----

/**
 * Get all settings, optionally filtered by section.
 */
export async function getSettings(section?: string): Promise<SettingRecord[]> {
  try {
    const where = section ? { section } : {};
    const rows = await db.appSetting.findMany({
      where,
      orderBy: { section: 'asc' },
    });
    return rows.map(row => ({
      key: row.key,
      value: row.value,
      section: row.section,
      description: row.description,
    }));
  } catch (error) {
    console.error('[SettingsManager] getSettings error:', error instanceof Error ? error.message : String(error));
    return [];
  }
}

/**
 * Get a single setting value by key. Returns null if not found.
 */
export async function getSetting(key: string): Promise<string | null> {
  try {
    const row = await db.appSetting.findUnique({
      where: { key },
    });
    return row?.value ?? null;
  } catch (error) {
    console.error(`[SettingsManager] getSetting error for "${key}":`, error instanceof Error ? error.message : String(error));
    return null;
  }
}

/**
 * Get a setting value parsed as a number. Returns the fallback if not found or invalid.
 */
export async function getSettingAsNumber(key: string, fallback: number = 0): Promise<number> {
  const value = await getSetting(key);
  if (value === null) return fallback;
  const parsed = parseFloat(value);
  return Number.isNaN(parsed) ? fallback : parsed;
}

/**
 * Get a setting value parsed as a boolean. Returns the fallback if not found.
 * Recognizes "true", "1", "yes" as truthy (case-insensitive).
 */
export async function getSettingAsBoolean(key: string, fallback: boolean = false): Promise<boolean> {
  const value = await getSetting(key);
  if (value === null) return fallback;
  return ['true', '1', 'yes'].includes(value.toLowerCase());
}

/**
 * Set a single setting value. Creates the setting if it doesn't exist.
 * Logs the change to the ConfigChange audit table.
 */
export async function setSetting(
  key: string,
  value: string,
  section: string,
  source: string = 'system'
): Promise<void> {
  try {
    // Fetch old value for audit log
    const existing = await db.appSetting.findUnique({ where: { key } });
    const oldValue = existing?.value ?? null;

    // Upsert the setting
    await db.appSetting.upsert({
      where: { key },
      update: { value, section, updatedAt: new Date() },
      create: { key, value, section },
    });

    // Write audit log (only if value actually changed)
    if (oldValue !== value) {
      try {
        await db.configChange.create({
          data: {
            section,
            key,
            oldValue,
            newValue: value,
            source,
          },
        });
      } catch (auditError) {
        console.error('[SettingsManager] Failed to write audit log:', auditError instanceof Error ? auditError.message : String(auditError));
      }
    }
  } catch (error) {
    console.error(`[SettingsManager] setSetting error for "${key}":`, error instanceof Error ? error.message : String(error));
    throw error;
  }
}

/**
 * Bulk update multiple settings in a single transaction.
 * Logs each change to the audit table.
 */
export async function bulkUpdateSettings(
  updates: SettingUpdate[],
  source: string = 'system'
): Promise<void> {
  try {
    await db.$transaction(async (tx) => {
      for (const update of updates) {
        // Fetch old value for audit
        const existing = await tx.appSetting.findUnique({ where: { key: update.key } });
        const oldValue = existing?.value ?? null;

        // Upsert
        await tx.appSetting.upsert({
          where: { key: update.key },
          update: { value: update.value, section: update.section, updatedAt: new Date() },
          create: { key: update.key, value: update.value, section: update.section },
        });

        // Audit log (only if changed)
        if (oldValue !== update.value) {
          await tx.configChange.create({
            data: {
              section: update.section,
              key: update.key,
              oldValue,
              newValue: update.value,
              source,
            },
          });
        }
      }
    });
  } catch (error) {
    console.error('[SettingsManager] bulkUpdateSettings error:', error instanceof Error ? error.message : String(error));
    throw error;
  }
}

/**
 * Initialize default settings on first run.
 * Only creates settings that don't already exist (won't overwrite user changes).
 */
export async function initDefaultSettings(): Promise<void> {
  try {
    for (const setting of DEFAULT_SETTINGS) {
      await db.appSetting.upsert({
        where: { key: setting.key },
        update: {}, // Do NOT update existing values
        create: {
          key: setting.key,
          value: setting.value,
          section: setting.section,
        },
      });
    }
    console.log(`[SettingsManager] Initialized ${DEFAULT_SETTINGS.length} default settings`);
  } catch (error) {
    console.error('[SettingsManager] initDefaultSettings error:', error instanceof Error ? error.message : String(error));
  }
}

/**
 * Export all settings as a flat key-value record.
 * Used for creating configuration profiles.
 */
export async function exportAllSettings(): Promise<Record<string, string>> {
  try {
    const rows = await db.appSetting.findMany({
      orderBy: { section: 'asc' },
    });
    const result: Record<string, string> = {};
    for (const row of rows) {
      result[row.key] = row.value;
    }
    return result;
  } catch (error) {
    console.error('[SettingsManager] exportAllSettings error:', error instanceof Error ? error.message : String(error));
    return {};
  }
}

/**
 * Import settings from a key-value record.
 * Optionally specify a source for audit logging.
 * Uses upsert so existing values are overwritten, new ones are created.
 */
export async function importSettings(
  data: Record<string, any>,
  source: string = 'import'
): Promise<number> {
  try {
    let importedCount = 0;

    await db.$transaction(async (tx) => {
      for (const [key, rawValue] of Object.entries(data)) {
        const value = String(rawValue ?? '');

        // Fetch old value for audit
        const existing = await tx.appSetting.findUnique({ where: { key } });
        const oldValue = existing?.value ?? null;

        // Determine section from key (use "imported" as fallback)
        // Try to find matching default section
        const defaultMatch = DEFAULT_SETTINGS.find(s => s.key === key);
        const section = existing?.section ?? defaultMatch?.section ?? 'imported';

        await tx.appSetting.upsert({
          where: { key },
          update: { value, section, updatedAt: new Date() },
          create: { key, value, section },
        });

        // Audit log
        if (oldValue !== value) {
          await tx.configChange.create({
            data: { section, key, oldValue, newValue: value, source },
          });
        }

        importedCount++;
      }
    });

    console.log(`[SettingsManager] Imported ${importedCount} settings from "${source}"`);
    return importedCount;
  } catch (error) {
    console.error('[SettingsManager] importSettings error:', error instanceof Error ? error.message : String(error));
    return 0;
  }
}

/**
 * Get all settings grouped by section.
 * Returns a record where keys are section names and values are arrays of settings.
 */
export async function getSettingsBySection(): Promise<Record<string, SettingRecord[]>> {
  try {
    const rows = await db.appSetting.findMany({
      orderBy: [{ section: 'asc' }, { key: 'asc' }],
    });

    const grouped: Record<string, SettingRecord[]> = {};
    for (const row of rows) {
      if (!grouped[row.section]) {
        grouped[row.section] = [];
      }
      grouped[row.section].push({
        key: row.key,
        value: row.value,
        section: row.section,
        description: row.description,
      });
    }
    return grouped;
  } catch (error) {
    console.error('[SettingsManager] getSettingsBySection error:', error instanceof Error ? error.message : String(error));
    return {};
  }
}
