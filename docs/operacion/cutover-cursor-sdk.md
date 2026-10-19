# Cutover de Cursor CLI a Cursor SDK

Este procedimiento es una acción de control-plane manual. No se ejecuta desde
tests, submit, review ni al cambiar `ai_dev_loop.yaml`. Exige un paquete aceptado,
autorización separada del operador y una ventana quiescente. Phase 24 continúa
sin ejecutarse y no es requisito para este cutover.

## 1. Cerrar o inmovilizar trabajo Cursor CLI anterior

Antes de instalar, inventaría runs y secuencias con la versión estable todavía
instalada:

```bash
ai_dev_loop scheduler list --output json
ai_dev_loop integration sequences list
ai_dev_loop scheduler status <run-id> --output json
ai_dev_loop scheduler sequence status <sequence-id> --output json
```

Termina o asienta con esa versión los runs Cursor CLI, waits, successors de
recovery, handoffs/checkpoints/replacements pendientes y definiciones preparadas
que contengan una hoja CLI. Si el operador decide abandonar uno, usa
`scheduler abort` y conserva sus cambios y artefactos. No borres ni conviertas
records. Después del cutover, las consultas siguen disponibles, pero
start/retry/review-retry/extend/materialización/handoff de Cursor CLI se rechazan.
Los records Codex-only, incluso con config v1 o un campo Cursor inerte, conservan
ejecución y recovery.

## 2. Confirmar quiescencia y tomar backup consistente

Deshabilita el timer solo si esa acción fue autorizada y comprueba que no haya
attempt owned activo ni hold incierto que todavía necesite reconciliación:

```bash
ai_dev_loop scheduler timer status --output json
ai_dev_loop scheduler list --output json
```

Un hold de identidad, terminación o dispatch incierto debe resolverse con el
runtime estable antes de instalar. Si sigue incierto, conserva su reserva y
evidencia y detén el cutover; no lances otro agente para adivinar su estado. Con el
writer detenido, toma una copia SQLite consistente mediante la API de backup de
SQLite (por ejemplo, `sqlite3 ... .backup`) y respalda juntos:

- `engine.sqlite3` desde esa copia consistente y `artifacts/`;
- configuración XDG y el `ai_dev_loop.yaml` aprobado;
- `$XDG_STATE_HOME/ai_dev_loop/cursor-sdk/` si ya contiene conversaciones nativas.

No copies un `engine.sqlite3-wal` activo como si fuese un snapshot consistente.
Mantén permisos restrictivos. Los stores JSONL son mutables y son autoridad para
replay de conversaciones SDK todavía recuperables: una política de retención no
debe borrarlos mientras exista trabajo o recovery asociado.

## 3. Instalar el artefacto aceptado y provisionar la credencial

Solo después de la aceptación completa y autorización de instalación, usa el
wheel aceptado y el extra exacto, nunca el checkout cambiante:

```bash
uv tool install --force \
  'ai-dev-loop[cursor-sdk] @ file:///ruta/absoluta/dist/ai_dev_loop-0.1.0-py3-none-any.whl'
ai_dev_loop --version
ai_dev_loop scheduler --help
```

Provisiona por separado
`$XDG_CONFIG_HOME/ai_dev_loop/credentials/cursor-default`: directorios `0700`,
archivo regular owned `0600`, una sola línea UTF-8 y ningún symlink. No pongas la
llave en YAML, argv, logs ni variables de entorno. Para rotarla, vuelve a dejar el
scheduler quiescente, reemplaza el archivo de forma local y atómica conservando
owner/mode, valida con un smoke autorizado y revoca la llave anterior fuera del
producto. `ai_dev_loop` no crea, rota ni revoca credenciales.

Instalar no autoriza cambios de timer, `linger` o bus systemd. Para una acción de
timer aprobada sigue [Timer del scheduler en WSL](timer-systemd-wsl.md).

## 4. Verificar lecturas y convertir inputs explícitamente

Primero verifica que historial, nombres, filtros, paginación y contenido
deliberado siguen legibles sin modificar records:

```bash
ai_dev_loop scheduler list --output json
ai_dev_loop integration runs list --limit 20
ai_dev_loop dashboard serve --port 8765
```

El Dashboard es una proyección local read-only; iniciar el servidor real también
requiere decisión operativa. Sus definiciones de actividad no cambian: alcance
por attempt, rango temporal inclusivo/exclusivo documentado, selección por ID,
aliases scheduler-owned y filtros/paginación estables. Las métricas de uso son
read-only, declaran cobertura (`complete`, `partial`, `unknown` o
`not_applicable` cuando no hubo inferencia) y separan
roles `implementer` y `reviewer`, incluidos ambos roles Codex. No son billing y no
afectan review, Git, timeouts ni recovery.

Convierte cada repositorio Cursor objetivo a config v2 y cada secuencia con
Cursor a manifest v4, con runtime/modelo/parámetros, `credential_ref`, sources y
sandbox explícitos. Luego crea un submit/prepare fresco con `short_name` y modelo
y razonamiento del reviewer explícitos. Un manifest v3 nuevo solo es válido si
es Codex-only. Instalar el paquete no actualiza una definición CLI ya preparada.
La edición fuente de `ai_dev_loop.yaml` requiere aceptación manual independiente.

## 5. Smoke real opcional y rollback

Un smoke real no forma parte de pytest ni queda autorizado por este documento.
Si el operador lo aprueba, usa un repositorio sintético, HOME/XDG/store aislados,
prompt acotado y un único run SDK. Registra resultado y uso sin copiar la
credencial. Mantén ese resultado separado de la integración con fakes.

Una instalación interrumpida se recupera desde el backup consistente; no lances
trabajo para inferir qué versión quedó activa. Después de crear records SDK, el
rollback necesita un paquete reader-compatible y otra ventana quiescente. Volver
solo a un reader pre-Phase-25 es inseguro. No borres ledger, artifacts, store ni
credenciales como parte del rollback; la limpieza es una autorización destructiva
distinta.
