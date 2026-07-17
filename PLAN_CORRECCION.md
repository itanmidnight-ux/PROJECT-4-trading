# Plan de corrección — Auditoría final (aplicar con Sonnet 5)

Auditoría hecha sobre `main` @ `3abad49`. Estado general: 156 tests pasan,
ruff/shellcheck limpios, ciclo `--start/--status/--stop` verificado en vivo,
dashboard responsive verificado con Playwright, persistencia OK, estrategia
funcional (rentabilidad honestamente documentada en README, ~breakeven en
backtest — eso no es un bug). Se encontraron **2 bugs críticos y 1 menor**.

## BUG 1 (CRÍTICO): install.sh falla en Kali/Ubuntu corriendo como root

`install.sh` líneas ~372 y ~376:
```bash
if ! timeout 1200 "$SUDO" apt-get update -y; then
if ! timeout 1800 "$SUDO" apt-get install -y --no-install-recommends \
```
Cuando el usuario es root, `SUDO=""` y el `"$SUDO"` **entre comillas** se
expande a un argumento vacío: `timeout 1200 "" apt-get ...` intenta ejecutar
el comando `""` y falla siempre. Kali corre como root habitualmente → la
instalación completa aborta en el paso 1/5. (Se introdujo al "arreglar"
SC2086 citando la variable; aquí el word-splitting es deseado.)

**Fix exacto** (en ambas líneas):
```bash
# shellcheck disable=SC2086  # $SUDO debe desaparecer (no ser un arg vacio) cuando se corre como root
if ! timeout 1200 $SUDO apt-get update -y; then
```
(ídem con 1800 para el install). Verificar después: `shellcheck install.sh`
limpio, y probar ambos caminos:
`SUDO="" bash -c 'timeout 5 $SUDO true'` y con `SUDO="env"` como stand-in.

## BUG 2 (CRÍTICO): crash-loop infinito del motor si falta pandas

En una instalación Termux "parcial" (sin pandas — resultado documentado y
soportado), pulsar "Iniciar motor" en el dashboard lanza
`core/engine_supervisor.py`, que ejecuta `main.py`; `main.py` importa
`core/market_data.py` → `import pandas` → `ImportError` inmediato → el
supervisor lo reintenta **para siempre** (backoff 2s→60s, sin límite). El
dashboard muestra "Motor corriendo" (el supervisor vive) mientras el motor
muere en bucle. Dos fixes complementarios:

**2a. `dashboard.py` — pre-chequeo en `api_engine_start`** (antes del Popen):
```python
probe = subprocess.run(
    [sys.executable, "-c", "import pandas, numpy"],
    capture_output=True, timeout=30,
)
if probe.returncode != 0:
    return jsonify({"ok": False, "error":
        "Faltan pandas/numpy en esta maquina - el motor no puede correr aca. "
        "En Termux esto es lo esperado si install.sh no pudo compilarlos: "
        "usa esta maquina solo como visor y corre el motor en una Kali/Ubuntu."}), 409
```
El frontend ya muestra `result.error` por consola y revierte el botón
(`initEngineButton`) — opcionalmente mostrar el error en un elemento visible,
pero mínimo viable: el 409 evita el loop.

**2b. `core/engine_supervisor.py` — rendirse tras fallos rápidos seguidos**:
en `main()`, contar corridas consecutivas con `ran_for < 10`; al llegar a 5,
escribir al log `"[engine_supervisor] main.py fallo 5 veces seguidas en <10s
- rindiendome para no quedar en bucle infinito. Revisa data/logs/engine.stdout.log"`
y salir del while (exit limpio). Un `ran_for >= 60` ya resetea backoff;
resetear también ese contador ahí. El pidfile se auto-sanea vía
`_engine_pid()` → el dashboard pasa a "Motor detenido" solo.

**Tests** (en `tests/test_engine_supervisor.py`, seguir el patrón existente
de fake `main.py` + `monkeypatch.setattr(sup, "ROOT", tmp_path)`):
- fake main que siempre sale al instante → `sup.main()` termina solo (no
  colgar el test), log contiene "rindiendome"/mensaje de give-up, y el
  contador de corridas es exactamente 5.
- Test para 2a en `tests/test_dashboard_engine_control.py`: monkeypatch de
  `subprocess.run` devolviendo returncode=1 → el POST da 409 y NO llama Popen.
  OJO: `dmod.subprocess` ES el módulo stdlib — capturar la referencia real
  antes de parchear (ya hay precedente en ese archivo, leer su comentario
  sobre `_real_popen`).

## BUG 3 (MENOR): pip de herramientas de build sin límite ni salida

`install.sh` línea ~249:
```bash
if pip install -q Cython meson meson-python wheel setuptools 'versioneer[toml]' 2>/dev/null; then
```
Sin timeout y con errores silenciados. Fix: `timeout 600 pip install ...`
y quitar `2>/dev/null` (mantener el fallback `warn` existente si falla).

## Reglas de aplicación (obligatorias)

1. Trabajar en `claude/gold-trading-bot-xauusd-qmu0v4`.
2. Tras los cambios: `shellcheck run.sh install.sh` limpio; ruff con el
   select exacto del CI (`--select F,B,SIM,C4 --ignore SIM118` sobre
   `core/ bridge/ tests/ scripts/ main.py dashboard.py`); `./run.sh verify`
   completo en verde (crear `.env` desde `.env.example` si hace falta y
   **borrarlo antes de commitear**; revisar `git status` para no colar
   artefactos de `data/`).
3. Commit descriptivo, push a la rama, merge a `main` con
   `--no-ff --no-gpg-sign`, re-verificar en `main`, push, volver a la rama.
4. Último paso del plan: **borrar este archivo** (`PLAN_CORRECCION.md`) en el
   mismo commit final o en uno propio — es un documento de trabajo, no del
   producto.

## Verificado OK en esta auditoría (no tocar)

- Detección de plataforma y caminos Termux/Debian-like/unknown de ambos .sh.
- `--skip-pandas`/`--retry-pandas` + marcador de fallo persistente.
- `--no-build-isolation` condicionado a numpy-desde-pkg (causa raíz Bionic
  documentada en install.sh).
- Parada limpia sin zombies (SIGCHLD reaper), kill-switch visible en
  `--status`, persistencia de Settings/pausa, dashboard responsive.
