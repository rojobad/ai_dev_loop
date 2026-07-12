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

## Riesgos residuales documentados

- Hook trust sigue siendo `unknown` desde CLI.
- Un override explicito puede no estar disponible para la cuenta o la version instalada de Codex CLI.
- Cambiar entre familias GPT-5.5 y GPT-5.6 puede intentar compactacion previa en versiones validadas; no ocurre universalmente.
- Temporales DrvFS pueden romper pytest capture.
- No hay comando destructivo de cleanup; limpieza es manual.
