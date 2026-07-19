# Trazabilidad de fases

La documentacion final esta organizada por uso, no por fase. Esta pagina resume que aporto cada fase y donde se reflejan esos requisitos.

## Fase 0

Hallazgos principales:

- WSL2, `git`, `agent` y `codex` disponibles.
- `uv` es la ruta recomendada para Python 3.11+ sin tocar Python del sistema.
- Cursor acepta prompt como argumento posicional en modo `agent -p`.
- `agent create-chat`, `agent models` y `agent status --format json` existen.
- Codex requiere `--cd` y `--sandbox` antes de `resume`.

Documentado en:

- [Instalacion](../guia/instalacion.md)
- [Referencia CLI](cli.md)
- [Configuracion](configuracion.md)

## Fase 1

Implemento paquete Python, CLI base, XDG paths, config, state, Git safety y `prepare`.

Documentado en:

- [Configuracion del repositorio](../guia/configuracion-repositorio.md)
- [Flujo de handoff](../guia/flujo-handoff.md)
- [Estado y artefactos](../operacion/estado-artefactos.md)

## Fase 2

Implemento preflight de `start`, locks, probes, creacion/reuso de Cursor chat y primer turno Cursor.

Documentado en:

- [Ejecutar runs](../operacion/prepare-start-resume-abort.md)
- [Seguridad y privacidad](../operacion/seguridad-privacidad.md)
- [Troubleshooting](../operacion/troubleshooting.md)

## Fase 3

Implemento staging controlado con `git add -A`, artefactos de diff staged y metadata de iteracion.

Documentado en:

- [Estado y artefactos](../operacion/estado-artefactos.md)
- [Seguridad y privacidad](../operacion/seguridad-privacidad.md)

## Fase 4

Implemento review Codex reanudando la sesion exacta, schema JSON, event log estructurado, reportes y fix prompt persistido.

Documentado en:

- [Ejecutar runs](../operacion/prepare-start-resume-abort.md)
- [Estado, logs e inspeccion](../operacion/observabilidad.md)
- [Seguridad y privacidad](../operacion/seguridad-privacidad.md)

## Fase 5

Implemento el loop completo bounded review/fix y `resume` real.

Documentado en:

- [Ejecutar runs](../operacion/prepare-start-resume-abort.md)
- [Estado y artefactos](../operacion/estado-artefactos.md)
- [Troubleshooting](../operacion/troubleshooting.md)

## Fase 6

Implemento `abort`, metadata de proceso activo, abort request y preservacion de repo/artefactos.

Documentado en:

- [Ejecutar runs](../operacion/prepare-start-resume-abort.md)
- [Estado y artefactos](../operacion/estado-artefactos.md)
- [Desinstalacion y limpieza](../operacion/desinstalacion-limpieza.md)

## Fase 7

Implemento integracion global WSL: skill, hook `SessionStart`, merge seguro de `hooks.json`, status, uninstall y doctor.

Documentado en:

- [Elegir target](../integraciones/seleccion-target.md)
- [WSL CLI](../integraciones/wsl-cli.md)
- [Confianza de hooks](../integraciones/confianza-hooks.md)

## Fase 7.5

Implemento `codex-desktop-wsl`, deteccion de home Windows, comando hook via `wsl.exe`, puente `sessions/from-desktop` y comandos `integrations sessions`.

Documentado en:

- [Codex Desktop + WSL](../integraciones/codex-desktop-wsl.md)
- [Puente de sesiones](../integraciones/puente-sesiones.md)
- [Sesiones Codex Desktop y WSL](codex-desktop-wsl-sessions.md)

## Fase 8

Implemento scaffold MkDocs, E2E fake acceptance, validacion de package install, smoke real acotado y fixes de probes Cursor.

Documentado en:

- Esta documentacion MkDocs.
- [Instalacion](../guia/instalacion.md)
- [Troubleshooting](../operacion/troubleshooting.md)

## Fase 9

Hizo opcionales `codex.review_model` y `codex.review_reasoning_effort`, con overrides independientes y compatibilidad para runs preparados bajo esa semantica.

Documentado en:

- [Referencia de configuracion](configuracion.md)
- [Configuracion del repositorio](../guia/configuracion-repositorio.md)
- [CLI](cli.md)
- [Troubleshooting](../operacion/troubleshooting.md)

## Fase 10

Implemento captura segura del modelo/reasoning de la sesion exacta durante `prepare`, procedencia independiente y paso explicito de ambos valores en cada review. Tambien agrega probes de compatibilidad y politica de updates para las CLIs WSL en `start`/`resume`.

Documentado en:

- [Referencia de configuracion](configuracion.md)
- [CLI](cli.md)
- [Estado y artefactos](../operacion/estado-artefactos.md)
- [Seguridad y privacidad](../operacion/seguridad-privacidad.md)
- [Troubleshooting](../operacion/troubleshooting.md)

## Fase 11

Implemento `ai_dev_loop recover` para crear sucesores auditables de runs `failed` elegibles tras Cursor + staging, sin mutar el origen ni el repositorio. Incluye dry-run, lineage, migracion Phase 9 vs runtime congelado Phase 10, e idempotencia.

Documentado en:

- [Ejecutar runs](../operacion/prepare-start-resume-abort.md)
- [CLI](cli.md)
- [Estado y artefactos](../operacion/estado-artefactos.md)
- [Troubleshooting](../operacion/troubleshooting.md)
- [Guia rapida](../guia/guia-rapida.md)

## Fase 12

Permite que Cursor mute el index durante correcciones, mantiene el checkpoint estricto pre-Cursor, normaliza siempre con `git add -A`, captura fingerprints post-Cursor, y extiende `recover` al checkpoint `staging` (con `--adopt-current-cursor-output` para historicos sin fingerprint).

Documentado en:

- [Ejecutar runs](../operacion/prepare-start-resume-abort.md)
- [Seguridad y privacidad](../operacion/seguridad-privacidad.md)
- [Estado y artefactos](../operacion/estado-artefactos.md)
- [CLI](cli.md)
- [Troubleshooting](../operacion/troubleshooting.md)

## Fase 13

Implemento recovery de limite de uso de Cursor: clasificacion conservadora desde stderr protegido, fingerprint de contenido parcial, sucesor con el mismo chat ID, modelo fallback congelado (`--cursor-model auto`), envelope de continuacion con el prompt exacto previo, y prompt interactivo opcional en TTY tras fallos de `start`/`resume`.

Documentado en:

- [Ejecutar runs](../operacion/prepare-start-resume-abort.md)
- [Troubleshooting](../operacion/troubleshooting.md)
- [Referencia CLI](cli.md)
- [Seguridad y privacidad](../operacion/seguridad-privacidad.md)
- [Estado, logs e inspeccion](../operacion/observabilidad.md)
- [Guia rapida](../guia/guia-rapida.md)
- [Flujo de handoff](../guia/flujo-handoff.md)
- [Referencia de configuracion](configuracion.md)
- [Estado y artefactos](../operacion/estado-artefactos.md)

## Fase 14

Implemento controller remoto y sesion reviewer aislada (A/B): `prepare --controller-session-id`, `launch` con worker local detachado, `controller status` read-only, skills `ai-dev-loop-handoff` y `ai-dev-loop-controller`, sin notificaciones automaticas y sin cambios al YAML del repo objetivo.

Documentado en:

- [Flujo de handoff](../guia/flujo-handoff.md)
- [Ejecutar runs](../operacion/prepare-start-resume-abort.md)
- [Codex Desktop + WSL](../integraciones/codex-desktop-wsl.md)
- [Instalacion](../guia/instalacion.md)
- [Troubleshooting](../operacion/troubleshooting.md)
- [Seguridad y privacidad](../operacion/seguridad-privacidad.md)
- [Estado, logs e inspeccion](../operacion/observabilidad.md)
- [Referencia CLI](cli.md)
- [Referencia de configuracion](configuracion.md)

## Fase 14.5

Corrige el contrato de staging de la iteracion inicial: el indice vacio/pre-staged se exige solo en el boundary pre-Cursor (`prepare`/`start`); tras Cursor (inicial o correccion) el index puede mutar. El orquestador sigue normalizando con `git add -A` y Codex revisa el snapshot completo. Extiende `recover` con `reason_code: initial_staging_failed` (fingerprint verificado, hashes de patch nulos, sin adopcion) para retomar staging sin re-ejecutar Cursor.

Documentado en:

- [Ejecutar runs](../operacion/prepare-start-resume-abort.md)
- [Seguridad y privacidad](../operacion/seguridad-privacidad.md)
- [Estado y artefactos](../operacion/estado-artefactos.md)
- [CLI](cli.md)
- [Troubleshooting](../operacion/troubleshooting.md)

## Fase 15

Ciclo opt-in post-PR con `gh` autenticado (sin tokens en YAML/state): `github doctor`, `pr-review create|status|continue|resume|abort`, commit/push no-force del patch staged aceptado, PR a `master`, polling de review del bot configurado, adjudicacion con la sesion Codex exacta, fix con el Cursor chat exacto, y parada con `@rojobad` ante hallazgos no aplicables/inciertos.

## Fase 15.5

Adopcion independiente de un PR ya abierto (`pr-review prepare` / `start` /
`set-cursor-model`) sin debilitar el path `create` de source-run. Prepare no
escribe en GitHub; start es la puerta explicita de escritura. El Cursor chat se
crea solo tras feedback externo all-actionable; el modelo Cursor puede cambiarse
antes de ese chat.

Documentado en:

- [Referencia CLI](cli.md)
- [Referencia de configuracion](configuracion.md)
- [Seguridad y privacidad](../operacion/seguridad-privacidad.md)

## Fase 15.6

Acuse best-effort (`eyes`) y finalizacion verificable sin hallazgos para el
worker de PR review (ciclos Phase 15 y 15.5). La finalizacion exige comentario
general del reviewer permitido, posterior al trigger, con prefijo configurado y
`Reviewed commit:` ligado al `bound_head_sha`; nunca completa por ausencia de
hilos ni por presencia/retirada de `eyes`. Automatic reviews de Codex permanece
fuera de este flujo.

Documentado en:

- [Referencia de configuracion](configuracion.md)
- [Flujo completo](../guia/flujo-completo.md)
- [Troubleshooting](../operacion/troubleshooting.md)

## Fase 15.7

Compatibilidad del schema de adjudicacion GitHub con el response-format de Codex
(sin `uniqueItems` en el transporte; unicidad en Pydantic) y
`pr-review recover` para sucesores inmutables tras rechazo
`invalid_json_schema` antes de side effects. Reutiliza PR/SHA/trigger/hilos/
sesion B/controlador A y nunca republica `@codex review`.

Documentado en:

- [Referencia CLI](cli.md)
- [Flujo completo](../guia/flujo-completo.md)
- [Troubleshooting](../operacion/troubleshooting.md)
- [Estado y artefactos](../operacion/estado-artefactos.md)

## Fase 15.8

Recuperacion de checkpoint local `reviewing` cuando Cursor/staging completaron
pero falta el artefacto `codex/reviews/NN.json`. El sucesor reintenta solo la
revision Codex B; no reejecuta Cursor ni republica el trigger.

## Fase 15.9

Continuidad del worker PR-review tras publicar un fix externo (el mismo proceso
sigue a polling sin auto-spawn), reset de `expected_eligible_thread_ids` por
ciclo externo, y `pr-review resume` para reenganchar un poller ausente/stale en
`awaiting_bot_review` con A/B exactos sin republicar `@codex review` (validacion
PR/head solo lectura permitida; sin writes GitHub, Cursor ni Codex).

Documentado en:

- [Referencia CLI](cli.md)
- [Flujo completo](../guia/flujo-completo.md)
- [Troubleshooting](../operacion/troubleshooting.md)
- [Estado y artefactos](../operacion/estado-artefactos.md)

## Fase 15.10

Recuperación histórica lineage-bound del freeze `expected_eligible_thread_ids`
heredado de una recovery `external_adjudication` de un ciclo anterior cuando el
ciclo actual ya publicó un marker válido. Tras un comentario exacto nuevo
`@rojobad /ai-dev-loop continue`, `pr-review continue` limpia solo ese freeze
obsoleto en el mismo run (PR/SHA/marker/A/B/Cursor intactos), reanuda el worker
y **no** republica `@codex review`. El drift legítimo del ciclo actual sigue
deteniendo el run.

Documentado en:

- [Referencia CLI](cli.md)
- [Flujo completo](../guia/flujo-completo.md)
- [Troubleshooting](../operacion/troubleshooting.md)
- [Estado y artefactos](../operacion/estado-artefactos.md)

## Fase 15.11

Extiende Phase 15.10 al lineage anidado de un solo salto: cuando el run actual
tiene recovery `reviewing` / `codex_review_result_artifact_missing`, se carga
exactamente un source terminal con recovery `external_adjudication` verificada
(misma identidad PR/repo, mismos IDs esperados ya processed/resolved, ciclo
ancestral estricto menor). Sin recursión ni limpieza ambigua. Misma autorización
por comentario exacto nuevo; mismo run; sin republicar `@codex review`.

Documentado en:

- [Referencia CLI](cli.md)
- [Flujo completo](../guia/flujo-completo.md)
- [Troubleshooting](../operacion/troubleshooting.md)
- [Estado y artefactos](../operacion/estado-artefactos.md)

## Fase 15.12

Corrección externa en iteración nueva monotona (prompt `github-NN` exacto; nunca
staging sobre artefactos históricos) y `pr-review recover` para el checkpoint
`external_feedback_cursor` cuando el run falló en `fixing_external_feedback`
antes de que Cursor arrancara. Sucesor inmutable; `resume` abre Cursor antes de
staging; no re-adjudica ni republica `@codex review`.

Documentado en:

- [Referencia CLI](cli.md)
- [Flujo completo](../guia/flujo-completo.md)
- [Troubleshooting](../operacion/troubleshooting.md)
- [Estado y artefactos](../operacion/estado-artefactos.md)

## Fase 15.13

Interrupción tipada cuando `ssh-agent` no tiene identidad utilizable durante
publicación (`SshAgentNoIdentityError` en `pre_commit`) y `pr-review recover`
para el checkpoint `publication_pre_commit` en runs históricos `failed` con
evidencia durable de fase/patch/SHA/PR/hilos y revisión local sin hallazgos.
El `resume` del sucesor publica solamente; no abre agentes ni repite
adjudicación/replies/resolves/`@codex review`. Los `ValidationError` genéricos
siguen siendo terminales.

Documentado en:

- [Referencia CLI](cli.md)
- [Flujo completo](../guia/flujo-completo.md)
- [Troubleshooting](../operacion/troubleshooting.md)
- [Estado y artefactos](../operacion/estado-artefactos.md)

## Fase 15.14

Preflight de publicación que valida el `ssh-agent` efectivo de OpenSSH: resuelve
`IdentityAgent` con `ssh -G` sobre el destino del remote SSH (alias SCP o URL
`ssh://`), comprueba que el socket sea utilizable y ejecuta `ssh-add -l` sólo
con ese `SSH_AUTH_SOCK`. Si `IdentityAgent` está ausente o es `none`, usa un
`SSH_AUTH_SOCK` heredado válido. Fallos de formato/configuración siguen siendo
`ValidationError` terminales; sólo la ausencia de identidad conserva
`ssh_agent_no_identity` / `publication_pre_commit` de Phase 15.13.

Documentado en:

- [Referencia CLI](cli.md)
- [Flujo completo](../guia/flujo-completo.md)
- [Troubleshooting](../operacion/troubleshooting.md)

## Fase 15.15

Corrige el fingerprint de publicación tras una corrección externa aceptada:
actualiza `github_pr_review.staged_patch_sha256` al hash raw live del patch
staged (tras equivalencia normalizada con el artefacto de la última iteración)
antes de `publishing_external_fix`. En recovery histórico
`publication_pre_commit`, un hash GPR obsoleto no bloquea si el index live
coincide con `git/diffs/NN.patch` vía `normalize_patch_text` (sólo CRLF/newline
terminal); el sucesor adopta el hash live y el origen permanece inmutable. Un
cambio de contenido real sigue siendo drift. El `resume` publica solamente.

Documentado en:

- [Referencia CLI](cli.md)
- [Flujo completo](../guia/flujo-completo.md)
- [Troubleshooting](../operacion/troubleshooting.md)
- [Estado y artefactos](../operacion/estado-artefactos.md)

## Fase 15.16

Baseline limpio para el primer Cursor de cada ronda de feedback externo
(`external_cursor_iteration`): identidad Git, branch/HEAD enlazados e
indice/worktree limpios; no compara con un patch publicado anterior. Las
correcciones locales posteriores a un finding Codex siguen exigiendo
`validate_correction_pre_cursor` sobre el patch staged de la iteración
anterior. Recovery `external_feedback_cursor` ignora un directorio
`cursor/iterations/NN` vacío sin entrada durable ni archivos, y clasifica ese
checkpoint antes de `reviewing`.

Documentado en:

- [Referencia CLI](cli.md)
- [Flujo completo](../guia/flujo-completo.md)
- [Troubleshooting](../operacion/troubleshooting.md)
- [Estado y artefactos](../operacion/estado-artefactos.md)

## Fase 15.17

Checkpoint tipado `github_pr_review.external_adjudication` persistido justo
después de Codex B y antes de GraphQL/Cursor/replies/publicación. `pr-review
resume` hidrata solo el ciclo actual; `RecoveryState` queda como lineage y no
como guardia global de hashes. El worker, tras `resume_run` local, continúa
in-process a publicación/polling (`schedule_worker=False`). Replies no
accionables usan intents durables para evitar duplicados ante ambigüedad.

Documentado en:

- [Referencia CLI](cli.md)
- [Flujo completo](../guia/flujo-completo.md)
- [Troubleshooting](../operacion/troubleshooting.md)
- [Estado y artefactos](../operacion/estado-artefactos.md)

## Riesgos residuales documentados

- Hook trust sigue siendo `unknown` desde CLI.
- Un override explicito puede no estar disponible para la cuenta o la version instalada de Codex CLI.
- Cambiar entre familias GPT-5.5 y GPT-5.6 puede intentar compactacion previa en versiones validadas; no ocurre universalmente.
- Temporales DrvFS pueden romper pytest capture.
- No hay comando destructivo de cleanup; limpieza es manual.
- Capacidades de fork/mensaje A↔B dependen de la app Codex; si faltan, el handoff A/B se detiene sin inventar IDs.
- No hay notificaciones push automaticas desde el worker; el estado se consulta bajo demanda desde A.
