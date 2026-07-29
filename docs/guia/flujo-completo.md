# Flujo completo de trabajo

Esta guía describe el recorrido desde una conversación de diseño en un proyecto
target hasta un PR revisado por Codex. `ai_dev_loop` automatiza la ejecución y
las revisiones; no hace merge ni sustituye la última validación humana antes de
integrar en `master`.

## Roles

- **Usuario:** decide el alcance, aprueba el plan, autoriza crear/iniciar el
  ciclo de PR y resuelve los casos inciertos.
- **Codex A:** chat principal y controlador. Conserva la conversación de diseño
  y controla el run.
- **Codex B:** revisor técnico en el flujo A/B. Su sesión exacta se reanuda para
  todas las revisiones; debe permanecer inactiva mientras el worker corre.
- **Cursor:** implementa y corrige en un único chat por run.
- **Worker local:** conserva artefactos fuera del repositorio, controla los
  límites y ejecuta las acciones Git/GitHub autorizadas.

El flujo A/B es el recomendado. También se admite una única sesión Codex, pero
esa sesión debe quedar inactiva antes de iniciar un worker que vaya a reanudarla.

## 1. Preparar el repositorio una vez

El proyecto target debe contar con:

- un `ai_dev_loop.yaml` válido;
- plan y prompt separados, habitualmente bajo `docs/.../plans/`;
- un skill de revisión staged configurado en `codex.review_skill`;
- para el ciclo GitHub, `github.enabled: true`, el skill
  `github.external_review_skill`, `gh` autenticado y Git por SSH funcional;
- las integraciones globales instaladas y el hook `ai_dev_loop` confiado desde
  `/hooks` de la superficie Codex que se use.

No se guardan tokens, sesiones, prompts completos, diffs ni artefactos de los
agentes en el repositorio target. Esos datos viven bajo el estado XDG local.

Comprueba la configuración y, si se usará GitHub, el entorno antes de empezar:

```bash
ai_dev_loop config validate --repo /ruta/al/proyecto
ai_dev_loop github doctor --repo-path /ruta/al/proyecto
```

El `ssh-agent` con la llave cargada debe ser el que OpenSSH usará para el
remote (vía `IdentityAgent` efectivo o `SSH_AUTH_SOCK` heredado válido). El
worker detached no depende de heredar `SSH_AUTH_SOCK` si `IdentityAgent` apunta
al agente persistente. `gh` autentica la API GitHub; el push sigue usando SSH.

## 2. Diseñar el cambio con Codex

Abre un chat Codex en el repositorio target, discute el cambio y refina el
alcance. Antes de implementar, Codex debe inspeccionar las reglas del proyecto,
identificar riesgos y resolver los `OpenQuestions` con el usuario.

Cuando el cambio esté aprobado, pide un plan para Cursor. El resultado debe ser:

```text
<directorio-de-planes>/<plan>.md
<directorio-de-planes>/prompt_<plan>.txt
```

El segundo archivo contiene el prompt exacto de la primera ejecución de Cursor.
No se debe depender de la memoria de la conversación para reconstruirlo.

## 3. Preparar y lanzar el loop local

Después de aprobar plan y prompt, usa `$ai-dev-loop-handoff`. En A/B, A crea un
fork B y B ejecuta `ai_dev_loop prepare` con su sesión como revisor y con la
sesión de A como controlador. B devuelve un `run-id` y un comando similar a:

```bash
ai_dev_loop launch <run-id> --controller-session-id <sesion-A>
```

A ejecuta ese comando. No se usa `start` en B ni se continúa conversando en B
mientras el run está activo.

El worker valida rama, HEAD, estado Git, hashes de plan/prompt, compatibilidad de
herramientas y modelos antes de ejecutar:

```text
Cursor implementa
→ staging controlado
→ Codex B revisa el diff staged
```

### Decisiones de la revisión local

| Resultado | Acción |
| --- | --- |
| No hay hallazgos accionables y tests correctos | El run queda `completed`; los cambios aceptados siguen staged. |
| No hay hallazgos accionables, pero los tests fallaron o están bloqueados y Codex concluye que no corresponde corregirlos | El run queda `completed_with_residual_risk`; se conserva el riesgo documentado. |
| Hay hallazgos accionables | Codex genera el prompt de corrección; Cursor corrige en el mismo chat y se repite staging/revisión hasta el límite configurado. |
| Se alcanza el límite local | El run se detiene con los artefactos y el prompt de corrección preservados. |

El worker no corrige ni interpreta por sí mismo: transporta el prompt exacto que
redactó Codex.

### Interrupciones y recuperación local

Timeouts, límites de Cursor, modelos incompatibles, aborts, cambios manuales en
el worktree/index o fallos de procesos dejan un checkpoint seguro. Según el
estado, se usa `resume` o `recover`; no se deben repetir turnos terminados ni
cambiar la sesión Codex o el chat Cursor registrado. Un fallback de modelo Cursor
se registra explícitamente, en vez de seleccionarse de forma silenciosa.

## 4. Ciclo PR-review (motor SQLite v2)

Requiere `pr_review_v2.enabled: true`. Los runs v1 (`RunState.github_pr_review`) y
subcomandos retirados (`continue`, `recover`, `set-cursor-model`) no tienen soporte.

### Desde un run A/B completado (`source_run`)

```bash
ai_dev_loop pr-review create <source-run-id>
ai_dev_loop pr-review start <run-id>
```

`create` congela un `PreparedState` read-only desde el source (plan/prompt/patch
verificados). `start` es la unica puerta a efectos externos (publicacion, trigger,
supervisor).

### Desde un PR ya abierto (`existing_pr`)

```bash
ai_dev_loop pr-review prepare \
  --repo OWNER/REPO \
  --pr <numero> \
  --codex-session-id <sesion-B-exacta> \
  --plan <plan-rel-al-repo> \
  --prompt <prompt-rel-al-repo> \
  [--repo-path /ruta/al/proyecto]
ai_dev_loop pr-review start <run-id>
```

`prepare` valida binding PR/local read-only. `start` lanza o repara el supervisor
detached y aplica la transicion durable.

### Operacion y observabilidad

```bash
ai_dev_loop pr-review status <run-id> --output json
ai_dev_loop pr-review history <run-id> --limit 50 --output json
ai_dev_loop pr-review resume <run-id> [--confirm-user-continuation]
ai_dev_loop pr-review abort <run-id>
```

- `status` / `history`: salida redactada desde SQLite (sin prompts, patches, tokens
  ni session IDs completos).
- `resume` en `waiting_for_user` exige `--confirm-user-continuation`.
- Resiliencia diferida (crash recovery, `recover` publico, etc.): ver
  `PHASE_16_8_DEFERRED_ISSUES.md` — no implementada en Phase 16.9.

## 5. Cierre y merge

El ciclo automatizado termina cuando el estado durable indica completion segun las
reglas del reducer y los artefactos protegidos. Consulta:

```bash
ai_dev_loop pr-review status <run-id> --output json
```

La ultima revision humana y el merge a `master` permanecen fuera de la
automatizacion.

## Límites de seguridad permanentes

- No usar `--last` ni inferir sesiones Codex.
- No crear otro chat Cursor dentro del mismo run.
- No guardar credenciales GitHub en YAML o artefactos.
- No exponer prompts completos, transcript, parches, JSONL ni tokens en la salida
  normal.
- No hacer merge, force-push, reset, clean, stash ni unstage automático.
- No convertir comentarios inciertos en correcciones automáticas.
