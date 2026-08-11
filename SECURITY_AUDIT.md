# Security audit

Estado: revisado el 2026-08-11.

- No se detectaron secretos reales ni bases de datos privadas versionadas; `data/*.db` está ignorado.
- La configuración carga credenciales desde entorno y documenta tokens separados para dashboard y bridge.
- Las operaciones sensibles deben permanecer protegidas por autenticación y ejecutarse en red local o detrás de un proxy HTTPS.
- El proyecto usa subprocessos para tareas de trading/MT5; las listas de argumentos deben mantenerse sin `shell=True` ni concatenación de entrada de usuario.
- Validación conocida: 269 pruebas pasadas y 14 omitidas en la suite existente.

Pendientes operativos: definir tokens largos en producción, restringir host/orígenes del despliegue y revisar periódicamente dependencias.
