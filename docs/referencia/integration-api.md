# API local de integración

La superficie `integration` expone un contrato JSON estable para que un Bridge
futuro supervise el scheduler local sin acceder a SQLite ni rutas privadas.

## Versión del contrato

- Versión actual de la API: **1.0** (`apiVersion.major` / `apiVersion.minor`).
- Misma major: compatible; campos desconocidos se ignoran en consumidores.
- Major distinta: el consumidor debe detenerse con `UPDATE_REQUIRED` y no emitir
  más peticiones de recursos.

## Formato de respuesta

Todas las órdenes bajo `integration` escriben **un único documento JSON** en stdout.

Éxito:

```json
{
  "apiVersion": { "major": 1, "minor": 0 },
  "ok": true,
  "observedAt": "2026-09-18T12:00:00Z",
  "data": {},
  "error": null
}
```

Error:

```json
{
  "apiVersion": { "major": 1, "minor": 0 },
  "ok": false,
  "observedAt": "2026-09-18T12:00:00Z",
  "data": null,
  "error": { "code": "INVALID_ARGUMENT", "message": "explicación segura" }
}
```

Códigos estables y códigos de salida:

| Código | Salida |
|--------|--------|
| `INTERNAL_ERROR` | 1 |
| `INVALID_ARGUMENT` | 2 |
| `NOT_FOUND` | 3 |
| `UNSUPPORTED` | 4 |
| `DATA_INTEGRITY` | 5 |
| `IO_ERROR` | 6 |

Los errores de argumentos y órdenes desconocidas dentro de `integration` usan este
sobre; no modifican el formato de error humano del resto de la CLI.

## `integration info`

```bash
ai_dev_loop integration info [--output json]
```

- `--output` es una opción del subcomando `info` (no del grupo `integration`).
- Solo admite `json` (valor por defecto en `info`).
- No requiere ledger del scheduler, no crea directorios XDG y no sondea Codex.
- `data.aiDevLoopVersion` es la versión del paquete instalado.
- `data.capabilities` declara de forma honesta qué lecturas existen en esta
  versión (Phase 21.1: todas `false`).

## Convenciones compartidas (fases posteriores)

- Identificadores opacos en camelCase; marcas de tiempo UTC RFC3339.
- Paginación: `--offset` / `--limit` con metadatos `items`, `offset`, `limit`,
  `nextOffset`, `hasMore`.
- Lecturas de artefactos por trozos en base64 con metadatos de offset; sin API
  genérica de rutas arbitrarias.

Los esquemas empaquetados viven en `src/ai_dev_loop/schemas/integration-api-*.json`.
