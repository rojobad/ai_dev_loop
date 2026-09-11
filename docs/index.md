# ai_dev_loop

`ai_dev_loop` es un orquestador local para ejecutar un ciclo de desarrollo asistido por IA sin perder identidad de agentes ni auditabilidad.

El flujo separa responsabilidades:

- Codex mantiene el contexto arquitectónico, prepara el handoff y revisa los cambios.
- Cursor implementa y corrige en un único chat local.
- `ai_dev_loop` valida, persiste estado en el ledger central, ejecuta CLIs locales, hace staging y coordina el loop mediante el scheduler.

El resultado esperado de un run exitoso es simple: los cambios quedan staged en el repositorio objetivo, los reportes quedan guardados bajo XDG state, y el usuario decide si revisa, ajusta o commitea manualmente.

## Diagrama operativo

```text
Codex interactivo (controller A)
  plan + prompt aprobados
        |
        v
ai_dev_loop scheduler submit
  congela inputs, run queued en engine.sqlite3
        |
        v
ai_dev_loop scheduler start <run-id>
  autoriza el run desde la misma sesion controller
        |
        v
scheduler tick (manual o timer systemd)
  preflight, Cursor, staging, review, correcciones
        |
        v
Cursor CLI, mismo chat
  implementa o corrige
        |
        v
git add -A controlado
  patch staged y artefactos
        |
        v
Codex CLI resume <session-id>
  reviewer B creado una vez; misma sesion en correcciones
        |
        v
sin findings -> completed
con findings -> fix prompt exacto -> Cursor
limite alcanzado -> max_iterations_reached
```

## Por donde empezar

Para una instalacion nueva, sigue:

1. [Instalacion](guia/instalacion.md)
2. [Elegir target de integracion](integraciones/seleccion-target.md)
3. [Configuracion del repositorio](guia/configuracion-repositorio.md)
4. [Flujo de handoff](guia/flujo-handoff.md)
5. [Ejecutar runs con el scheduler](operacion/prepare-start-resume-abort.md)

Para depurar un problema existente, empieza por [Estado, logs e inspeccion](operacion/observabilidad.md) y [Troubleshooting](operacion/troubleshooting.md).

## Invariantes principales

- Todo corre localmente en WSL/Linux; no hay workers remotos.
- Cada run usa un solo Cursor chat.
- El reviewer B se crea una vez en el primer review y se reanuda exactamente en correcciones posteriores.
- Codex decide el resultado mediante JSON estructurado; el Markdown no se scrapea para tomar decisiones.
- `ai_dev_loop` no genera prompts de correccion; solo valida y reenvia el prompt devuelto por Codex.
- El loop no commitea, no pushea, no limpia, no resetea y no unstaged cambios.
- El estado del orquestador vive fuera del repositorio objetivo, bajo rutas XDG (`engine.sqlite3`, `artifacts/`, etc.).

## Validacion de la documentacion

```bash
uv sync --all-extras
uv run mkdocs build --strict
```
