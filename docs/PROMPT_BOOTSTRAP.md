# Prompt de arranque para cualquier IA (sesión con cero contexto)

Copiar/pegar al iniciar una sesión nueva con CUALQUIER agente que tenga
acceso al repo (Claude, Codex, Cursor, Gemini, etc.). El repo es
autodescriptivo; este prompt solo fuerza la orientación correcta y una
verificación antes de actuar.

---

## Prompt universal (copiar desde aquí)

```
Estás trabajando en el repositorio `concursos-tracker`. Tienes CERO contexto
previo: el repo es autodescriptivo y tu PRIMERA tarea es orientarte. No
edites nada todavía.

1. LEE en este orden: `AGENTS.md` (si eres Claude Code, `CLAUDE.md` — es la
   fuente única de reglas), después `PLAN_MAESTRO.md`, después `ROADMAP.md`.
   Arquitectura solo si la necesitas: `MANUAL_IMPLEMENTACION.md`.

2. UBICA EL ESTADO: en `PLAN_MAESTRO.md`, el primer paso NO marcado `✅` es
   donde está el proyecto. El §0 tiene los números verificados, los
   artefactos y los comandos exactos. Los badges del `README.md` te dicen la
   frescura del estado (badge "estado verificado").

3. PRUEBA DE ORIENTACIÓN — antes de tocar nada, repórtame en ≤10 líneas:
   (a) en qué fase/paso estamos y cuál es su PRUEBA de éxito textual;
   (b) qué ACCIONES concretas vas a ejecutar;
   (c) qué archivos son intocables;
   (d) qué decisiones pendientes requieren mi autorización.
   Todo citado del repo. Si no puedes citar algo, dilo — NO lo inventes.

4. EJECUTA el paso siguiendo su ENTRADA/ACCIONES/PRUEBA/SI FALLA al pie de
   la letra. Reglas duras: cero falsos positivos (1 FP = protocolo STOP),
   sin scorers numéricos, sin hardcodes por municipio/portal, tests
   RED→GREEN, corridas congeladas a directorio NUEVO en `staging/`,
   paid_calls=0 en evaluación.

5. CIERRE (obligatorio aunque el paso no termine): actualiza los docs vivos
   según `.claude/skills/actualizar-docs/SKILL.md` — README (tabla Status +
   badges con su fuente de verdad), ROADMAP (estados) y PLAN_MAESTRO §0
   (+ marcar `✅ (fecha)` SOLO si la PRUEBA se cumplió, con artefacto). Deja
   además un resumen: qué hiciste, números, y el paso exacto donde quedaste.
```

## Variante corta (Claude Code / Codex — sus reglas cargan solas)

```
Oriéntate: en PLAN_MAESTRO.md el primer paso sin ✅ es donde estamos; §0
tiene números y comandos. Antes de actuar repórtame: paso actual + su
PRUEBA, qué vas a hacer, intocables, y decisiones que requieren mi
autorización — todo citado del repo, sin inventar. Luego ejecuta el paso
(ENTRADA/ACCIONES/PRUEBA/SI FALLA) y cierra con /actualizar-docs.
```

## Por qué funciona

- **Orden de lectura fijo** → el agente no improvisa su modelo del proyecto.
- **"Primer paso sin ✅"** → posición inequívoca sin depender de memoria.
- **Prueba de orientación citada** → detectas en 10 líneas si el agente
  entendió o está alucinando, ANTES de que toque algo.
- **Cierre con docs vivos** → la sesión siguiente arranca igual de bien que
  esta, aunque esta muera a la mitad.
