# INC-2026-10-30-001: `integration` falla con `AttributeError: typer._click.exceptions has no attribute 'Exit'` y oculta el error original (typer 0.27.3)

- Fecha: 2026-10-30 (reporte de madrugada; reproducción 06:06–06:12 La Paz / 10:06–10:12 UTC)
- Detectado por: Diego (agente doctor de la Beelink), reporte INC-20261030-ADL-02, sin cambios en el host
- Reproducido y registrado por: Brusy (supervisión), solo lectura
- Host: Beelink, usuario `rodrigo`
- Instalación: `ai_dev_loop` 0.1.0 como uv tool en `~/.local/share/uv/tools/ai-dev-loop` (Python 3.12), instalada el 2026-10-29 11:44 La Paz desde `~/.local/state/ai_dev_loop/operator-backups/phase30-deploy-20261029T114210/dist/ai_dev_loop-0.1.0-py3-none-any.whl`
- Dependencias resueltas en la instalación: `typer` 0.27.3 (publicada en PyPI el 2026-10-06), sin paquete `click` separado (typer 0.27 incluye su copia de Click en `typer._click`)
- Repo: `pyproject.toml` declara `typer>=0.12` sin cota superior; `uv.lock` fija `typer` 0.26.8 y `click` 8.4.2; el `.venv` del repo tiene typer 0.26.8
- Estado: Abierto (decisión de Rodrigo). Sin runs activos; el loop no está afectado.

## Síntoma
Cualquier camino de error o de salida temprana dentro del grupo `ai_dev_loop integration` termina con un traceback de Rich y exit 1:

```
ai_dev_loop/commands/integration.py:129 in invoke  ->  return super().invoke(ctx)
ai_dev_loop/commands/integration.py:130 in invoke  ->  except click.exceptions.Exit:
AttributeError: module 'typer._click.exceptions' has no attribute 'Exit'
```

En lugar del envelope JSON `ok:false` con el código de error de la Integration API, el usuario ve el `AttributeError` y la excepción original queda enterrada al inicio de la cadena (`During handling of the above exception, another exception occurred`).

Comandos reportados por Diego:
- `ai_dev_loop integration dashboard summary` (con y sin `--project ai-dev-loop`)
- `ai_dev_loop integration runs --help`

## Excepción original oculta
Verificado en el traceback encadenado completo:
- `integration dashboard summary` sin rango: `MissingParameter: Missing parameter: from_utc`. El comando exige `--from` y `--to` (RFC3339). Con rango funciona: `ai_dev_loop integration dashboard summary --from 2026-10-29T00:00:00Z --to 2026-10-30T12:00:00Z [--project ai-dev-loop]` devuelve `ok:true`, exit 0.
- `integration runs --help` y `integration dashboard summary --help`: la ayuda se imprime completa; la salida normal de `--help` (señal `Exit` de Click) es la que dispara el fallo y produce exit 1.

Es decir, los comandos reportados no tienen un fallo funcional propio: el defecto está en el manejador de errores, que convierte un error de uso esperado (o un `--help`) en un crash.

## Cronología (La Paz = UTC-4)
| Hora La Paz | Hora UTC | Etapa | Evidencia |
|---|---|---|---|
| 2026-10-06 13:24 | 2026-10-06 17:24 | Publicación de `typer` 0.27.3 en PyPI | metadatos PyPI |
| 2026-10-29 11:42–11:44 | 2026-10-29 15:42–15:44 | Despliegue phase30: `uv tool install` del wheel; resuelve `typer` 0.27.3 | `uv-receipt.toml`, mtime de `site-packages/typer` |
| 2026-10-30 (madrugada) | 2026-10-30 | Diego detecta el traceback en `dashboard summary` y `runs --help` | INC-20261030-ADL-02 |
| 2026-10-30 06:06–06:12 | 2026-10-30 10:06–10:12 | Brusy reproduce en solo lectura e identifica la excepción original | este registro |

## RCA (Brusy, solo lectura, 2026-10-30)
Hechos verificados:
- `ai_dev_loop/commands/integration.py` importa `from typer import _click as click` y usa `click.exceptions.Exit` en cinco lugares: `_finalize_integration_cli_outcome` (l. 76), `IntegrationParseMixin.parse_args` (l. 111, `raise`), `IntegrationTyperGroup.invoke` (l. 130 `except`, l. 134 `raise`) y `run_integration_cli` (l. 726).
- En typer 0.27.3: `hasattr(typer._click.exceptions, "Exit")` es `False`; `Exit` vive en `typer.exceptions.Exit` (exportado como `typer.Exit`, subclase de `RuntimeError`). No hay `click` instalado en el entorno de la herramienta.
- En typer 0.26.8 (el `.venv` del repo, versión del `uv.lock`) `typer._click.exceptions.Exit` existe; por eso las pruebas del repo pasan y el defecto solo aparece en la instalación desplegada.
- Python evalúa la expresión de una cláusula `except` solo cuando hay una excepción en vuelo. Por eso los caminos felices funcionan y únicamente fallan los caminos con excepción: errores de parseo, `IntegrationApiError` (p. ej. recurso no encontrado), `--output` inválido y `--help` de subcomandos.
- No hay otros usos de `typer._click` ni de `click.exceptions.Exit` en el paquete fuera de `commands/integration.py`.

Causa probable: incompatibilidad entre el código del adaptador de la Integration API (escrito contra la API privada `typer._click` de typer 0.26.x) y typer 0.27.3, que la instalación como uv tool resolvió porque `pyproject.toml` no acota `typer` y `uv tool install` de un wheel no aplica `uv.lock`.

Factores contribuyentes:
- Dependencia de un módulo privado de typer (`typer._click`), sin garantía de estabilidad.
- Restricción `typer>=0.12` sin cota superior; el lock no viaja con el wheel.
- No hay prueba de humo de la CLI instalada (caminos de error y `--help`) después del despliegue.
- El manejador de errores puede fallar él mismo y ocultar la excepción original.

Por qué no se detectó: los comandos usados a diario (`runs list`, `dashboard projects`, `codex-capacity`) y los dashboards solo ejercitan caminos sin excepción.

## Impacto
Verificado el 2026-10-30 06:06–06:12 La Paz, solo lectura:

| Comando | Resultado |
|---|---|
| `scheduler list`, `scheduler status <run>`, `scheduler timeline <run>`, `scheduler sequence status <seq>` | OK, exit 0 |
| `scheduler status <inexistente>`, `scheduler sequence status <inexistente>` | Error controlado (`run not found` / `prepared sequence not found`), exit 1, sin traceback |
| `scheduler --help`, `scheduler list --help`, `--help` | OK |
| `integration codex-capacity` | OK (`ok:true`), exit 0 |
| `integration runs list`, `integration sequences list`, `integration dashboard projects` | OK, exit 0 |
| `integration dashboard summary --from … --to … [--project …]` | OK, exit 0 |
| `integration --help` | OK |
| `integration dashboard summary` sin `--from/--to` | Traceback `AttributeError`, exit 1 (original: `MissingParameter from_utc`) |
| `integration runs --help`, `integration dashboard summary --help` | Ayuda impresa y luego traceback, exit 1 |
| `integration run get <inexistente>`, `integration sequence get <inexistente>` | Traceback `AttributeError`, exit 1 (sin envelope JSON de error) |
| `integration codex-capacity --output text` | Traceback `AttributeError`, exit 1 (original: `invalid_argument`) |

- Loop (scheduler, timer, ticks, supervisión con `scheduler`/`sequence status`/`codex-capacity`): sin impacto. El grupo `scheduler` no usa este manejador.
- Dashboards 8765/8767: responden 200 según Diego. Inferencia no verificada: un consumidor de la Integration API que reciba un error recibe traceback en stderr y exit 1 en vez del envelope JSON con su código, por lo que pierde la clasificación del error.
- Diagnóstico: los errores de uso y de "no encontrado" del grupo `integration` dejan de ser legibles para humanos y para máquinas.

## Workaround
- Pasar siempre los argumentos obligatorios: `ai_dev_loop integration dashboard summary --from <RFC3339> --to <RFC3339> [--project ai-dev-loop]` (lo mismo para `dashboard runs`, `activity` y `token-usage`).
- No usar `--help` de subgrupos/subcomandos de `integration` para descubrir opciones; la ayuda sí se imprime antes del traceback, así que puede leerse ignorando el error. Alternativa: leer las firmas en `src/ai_dev_loop/commands/integration.py`.
- Ante un traceback con este `AttributeError`, leer la primera excepción de la cadena: es el error real.
- No se cambió ninguna versión instalada. Reinstalar con `typer` 0.26.8 (versión del lock) eliminaría el síntoma, pero cambia el despliegue y requiere decisión de Rodrigo.

## Correcciones propuestas (no aplicadas)
| Corrección | Esfuerzo |
|---|---|
| Reemplazar `click.exceptions.Exit` por `typer.Exit` (o resolver la clase de forma compatible con 0.26 y 0.27) en los cinco usos de `commands/integration.py` | S |
| Acotar `typer` en `pyproject.toml` (p. ej. `>=0.26,<0.28` tras validar) y/o instalar el uv tool respetando el lock (constraints exportadas de `uv.lock`) | S |
| Prueba que ejerza caminos de error y `--help` del grupo `integration` contra la versión de typer resuelta en el wheel | S |
| Prueba de humo post-despliegue de la CLI instalada (un `--help` de subcomando y un error de uso por grupo) | S |
| Dejar de depender del módulo privado `typer._click` (usar API pública de typer o declarar `click` explícitamente) | M |
| Que el manejador de errores no pueda ocultar la excepción original (fallback que emita el envelope `internal` con la clase original) | S |

(S ≤ 2 días; M 3–5 días; L ≥ 1 semana)

## Acciones de supervisión
Solo lectura: ejecución de los comandos listados, lectura de `integration.py` instalado, `uv-receipt.toml`, `pyproject.toml` y `uv.lock`, e introspección de typer con `python -B -c` (sin escribir bytecode). Sin instalaciones, cambios de versión, reinicios de servicios ni cambios de código. El registro se hizo en un worktree aparte (`~/project/ai_dev_loop_worktrees_incidentes/inc-2026-10-30-001`); el checkout principal `~/project/ai_dev_loop` (rama `main`, HEAD `2d0018a`, limpio) no se modificó.

## Relacionados
- Ninguno con la misma causa. Es el primer incidente registrado sobre dependencias de la instalación desplegada.
