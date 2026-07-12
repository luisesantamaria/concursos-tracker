# Prompt para arrancar la nueva sesión (copiar-pegar todo lo de abajo)

---

Continuamos el proyecto **Concursos Tracker** (motor de descubrimiento de índices de
concursos públicos y processos seletivos de municipios de RS). Yo soy Luis: corro todo
local en Brasil, soy el único editor, pusheo a GitHub. Respóndeme en **español**.

**ANTES DE TOCAR NADA, ponte en contexto:**
1. `git pull` en la rama `claude/skill-files-accuracy-vd6uyt` (working dir:
   `C:\Users\Luis Santamaria\Documents\PC\Claude\Concursos Tracker\concursos-tracker`).
2. Lee **completo** el archivo `HANDOFF_SESION_grounded_chrome.md` en la raíz del repo —
   ahí está TODO: dónde vamos (pleno 407, 81.9%), qué construimos, los aprendizajes, y la
   tarea pendiente al detalle. También tienes contexto en tu memoria
   (`project_grounded_verify.md`).

**DÓNDE QUEDAMOS / QUÉ VAMOS A HACER AHORA — MANO NEGRA TOTAL en Claude-in-Chrome:**

Tu instrucción exacta de la sesión anterior, que retomamos tal cual:

> "ok, no te estoy dando el claude chrome solo para verificar, sino para que encuentres la
> correcta y la confirmes. que des click a los menús hasta encontrar la url correcta MANO
> NEGRA TOTAL como lo habíamos hecho antes. de buscar en Google e ir más profundo hasta
> encontrar la url correcta."

O sea: para cada municipio pendiente, NO te limites a verificar la URL guardada — si no
sirve, **busca la correcta**: navega los menús del sitio oficial, busca en Google
(`"{municipio} RS concursos públicos prefeitura"`), entra más profundo, hasta dar con el
**índice real** del bucket. Cuando lo encuentres, **confírmalo** (aplícalo al CSV en modo
monótono — solo sube probable→confirmado, nunca baja — con nota `rev_humana(Chrome)`, y
commit). Como cuando hallamos `derrubadas-rs.com.br/site/...` en Google.

**Lista pendiente (sección 4 del handoff):**
- **Vacaria** (C) — quedó casi confirmado: es el índice oficial combinado, vacío ahora
  (como Itati) → candidato a CONFIRMAR. Empieza por aquí.
- Luego los 10: **Canoas** (P), **Caraá** (C), **Dom Pedrito** (P), **General Câmara** (P),
  **Gentil** (C), **Jaquirana** (P), **Parobé** (C), **São José do Norte** (P),
  **São Nicolau** (C), **Cruz Alta** (C).

**Criterio (reglas del proyecto):** confirmar solo índices que listan MÚLTIPLES
concursos/PSS del **tipo correcto**. Rechazar PDF/edital individual/licitação/pregão/
dispensa/atos de nomeação/concurso cultural. Índices oficiales VACÍOS pero con estructura
correcta = válidos (criterio Itati/Vacaria). **Cero falsos positivos** (precisión > cobertura).

**Truco técnico Chrome:** `get_page_text` solo agarra menús en estos sitios; usa
`javascript_tool` para extraer el contenido principal:
```js
(()=>{const el=document.querySelector('main,#conteudo,.conteudo,#content,.content,article')||document.body;return el?el.innerText.replace(/\s+/g,' ').trim().slice(0,700):'(vacio)';})()
```
Para SPA: navega primero, ejecuta el JS en llamada SEPARADA (batch navigate+JS falla por
timing). Si la navegación queda "pegada", re-navega.

**Primer paso concreto:** haz `git pull`, lee el handoff, **reconecta Chrome**
(`tabs_context_mcp`), y arranca confirmando Vacaria; luego sigue con los 10 pendientes.
Repórtame cada veredicto y aplica los válidos. Meta realista: ~412-415 plenos (~83%).
