# RECO-TRADING v4 (Windows-first)

Este proyecto queda preparado para ejecutarse en **Windows** con archivos `.bat`.

## Flujo principal en Windows
1. `install.bat`
2. `run.bat`

Para detener:
- `stop.bat`

## Brokers MT5 por modo
- **Demo / pruebas:** `MetaQuotes-Demo`
- **Real:** `Weltrade-Live` (o el servidor exacto entregado por Weltrade)

El sistema usa este perfil automáticamente en `weltrade_mt5` según el modo Demo/Real.

## Configuración en Dashboard
Ruta: **Settings > Broker Credentials**

Campos:
- MT5 Login
- MT5 Password
- Cuenta Demo/Live
- MT5 Server (opcional, si se deja vacío se autoasigna por modo)
- Bridge URL (`http://127.0.0.1:5001` por defecto)

## Compatibilidad de scripts
- Scripts primarios de operación: `.bat`
- Los `.sh` quedaron como wrappers de compatibilidad heredada.
