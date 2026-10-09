# Vinted Arbitrage Radar

Detección de oportunidades de arbitraje de **electrónica** en Vinted (consolas, GPUs, smartphones,
cámaras, audio) frente al precio real del mercado secundario.

> Lee primero **[ARCHITECTURE.md](ARCHITECTURE.md)**: riesgos, viabilidad, estrategia antibot,
> método de valoración y roadmap.

## Estado

| Módulo | Estado |
|---|---|
| 1 · Ingesta (Vinted catalog API, resiliencia completa) | ✅ |
| 2 · Normalizador (núcleo: SKU, estado, accesorios, señales de alarma) | ✅ núcleo |
| 3 · Valoración (núcleo: comisiones, margen, precio máximo de compra) | ✅ núcleo |
| 4 · Alertas Telegram/Discord | ⏳ |
| 5 · CLI avanzada / dashboard | ⏳ |

## Puesta en marcha

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # o: pip install -r requirements-dev.txt
cp .env.example .env              # ajusta umbrales, dominio, etc.

pytest                            # tests unitarios (sin red)
vinted-arb init-db                # crea data/vinted_arb.sqlite3
vinted-arb poll --once            # una pasada por todas las búsquedas de config/searches.yaml
vinted-arb poll                   # bucle continuo con planificación + jitter (Ctrl+C para parar)
```

Utilidades:

```bash
# Desglose de margen de una operación concreta
vinted-arb fees --buy 220 --sell 300 --channel wallapop

# Precio máximo de compra para cumplir ≥30 € netos y ROI ≥20 %
vinted-arb fees --sell 400 --min-net 30 --min-roi 0.20

# Depurar el normalizador
vinted-arb parse-title "PS5 Slim digital 1TB como nueva con caja y factura" --status "Muy bueno"
```

Test de contrato contra Vinted real (requiere red, ejecútalo en tu máquina):

```bash
pytest -m live
```

## Principios de operación

- **Bajo perfil**: ritmo humano, una sesión coherente, *circuit breaker* que **se detiene** ante
  captcha/403. Sin resolución de captchas, sin rotación de IPs, sin automatizar cuentas.
- **Ingesta anónima**: nunca uses tu cuenta personal de Vinted para la ingesta.
- **Modo sombra primero**: 2–4 semanas detectando sin comprar para validar KPIs (ARCHITECTURE.md §2.4).
