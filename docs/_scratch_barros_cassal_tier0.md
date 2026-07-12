# Diagnóstico Tier 0 — Barros Cassal

## Causa raíz

El candidato grounded sí llega a la confirmación HTTP: `tier2_find_site_grounded`
reduce la URL a su dominio base en `scripts/fase2_municipios/cascade_municipios.py:1047-1058`
y llama a `fetch_page` en `scripts/fase2_municipios/cascade_municipios.py:1062-1067`.
Un HTTP 200 produce `Page.ok == True` por
`scripts/fase2_municipios/cascade_municipios.py:129-131`, por lo que no se descarta
por status ni por redirect.

El descarte ocurre después, en
`scripts/fase2_municipios/cascade_municipios.py:1068-1076`: antes del fix la página
sólo se aceptaba
si `score_site_page(...) >= 5`. Ese scorer depende del HTML estático: suma por el
nombre completo del municipio o por la palabra `prefeitura` en
`scripts/fase2_municipios/cascade_municipios.py:420-430`. Un dominio municipal
`*.rs.gov.br` sólo aporta 3. Por tanto, la home 200 de Barros Cassal, servida como
shell de aplicación con título genérico y sin esas cadenas en el HTML visible,
queda con 3 y la función retorna `None`.

Ese `None` llega a `process_municipio`, que escribe `site_not_found` y
`tier0_failed` en `scripts/fase2_municipios/cascade_municipios.py:2023-2026`.
Esta es la causa raíz: la confirmación del dominio base confunde ausencia de
señales visibles en HTML estático con inexistencia del sitio, aun cuando el
hostname municipal oficial coincide inequívocamente con el municipio y responde
200. No es un fallo de deduplicación, normalización, redirect ni status.

## Hipótesis descartadas

- **Normalización `http`/`https`, `www` o `/`:** `clean_url` conserva una URL
  válida y la reducción grounded reconstruye `scheme://host/`
  (`scripts/fase2_municipios/cascade_municipios.py:1050-1058`).
- **Redirect 301/302:** `fetch_page` usa `allow_redirects=True`
  (`scripts/fase2_municipios/cascade_municipios.py:221`) y `Page.ok` admite todo
  status final entre 200 y 399 (`scripts/fase2_municipios/cascade_municipios.py:129-131`).
- **Eliminación previa del único candidato:** la deduplicación es por hostname y
  conserva la primera URL (`scripts/fase2_municipios/cascade_municipios.py:1048-1058`).
- **Timeout/excepción mal clasificada:** el caso anclado entrega HTTP 200; alcanza
  el scorer y se descarta por el umbral, no por la rama de excepción de
  `fetch_page` (`scripts/fase2_municipios/cascade_municipios.py:232-237`).
