# Flujo completo de trabajo

Esta guia describe el recorrido desde una conversacion de diseño en un proyecto
target hasta cambios staged y revisados por Codex. `ai_dev_loop` automatiza la
ejecucion y las revisiones mediante el scheduler central; no hace commit ni
sustituye la ultima validacion humana antes de integrar.

## Roles

- **Usuario:** decide el alcance, aprueba el plan y autoriza submit/start del run.
- **Codex A:** chat principal y controlador. Conserva la conversacion de diseño
  y controla el run.
- **Codex B:** revisor tecnico creado una vez por el scheduler en el primer review.
  Su sesion exacta se reanuda en todas las revisiones posteriores.
- **Cursor:** implementa y corrige en un unico chat por run.
- **Scheduler:** conserva artefactos fuera del repositorio, controla los limites y
  ejecuta las acciones Git autorizadas (staging normalizado con `git add -A`).

## 1. Preparar el repositorio una vez

El proyecto target debe contar con:

- un `ai_dev_loop.yaml` valido;
- plan y prompt separados, habitualmente bajo `docs/.../plans/`;
- un skill de revision staged configurado en `codex.review_skill`;
- las integraciones globales instaladas y el hook `ai_dev_loop` confiado desde
  `/hooks` de la superficie Codex que se use.

No se guardan tokens, sesiones, prompts completos, diffs ni artefactos de los
agentes en el repositorio target. Esos datos viven bajo el estado XDG local
(`engine.sqlite3`, `artifacts/`).

```bash
ai_dev_loop config validate --repo /ruta/al/proyecto
```

## 2. Diseñar el cambio con Codex

Usa Codex en el repositorio target para discutir el cambio, aprobar un plan y
redactar el prompt exacto para Cursor. El skill `ai-dev-loop-handoff` guia el
submit desde la sesion controller A.

## 3. Congelar y autorizar el run

```bash
ai_dev_loop scheduler submit ... --controller-session-id ... \
  --codex-review-model ... --codex-review-reasoning-effort ... \
  --output json < prompt.txt

ai_dev_loop scheduler start <run-id> --controller-session-id ...
```

## 4. Ejecutar ticks

```bash
ai_dev_loop scheduler tick
```

O instala y habilita el timer systemd solo tras aceptacion manual independiente.

## 5. Observar y cerrar

```bash
ai_dev_loop scheduler status <run-id>
ai_dev_loop controller status --controller-session-id ... --repo-path ...
ai_dev_loop scheduler abort <run-id>   # si hace falta cancelar
```

Los cambios quedan staged en el repositorio. El usuario revisa y commitea manualmente.

## Estado legacy

Los flujos `prepare`/`start`/`pr-review` y el arbol `runs/` fueron retirados.
Para eliminar estado legacy de forma controlada, consulta
[Desinstalacion y limpieza](../operacion/desinstalacion-limpieza.md).
