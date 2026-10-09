# Vinted Arbitrage Radar

Detección de oportunidades de arbitraje de **electrónica** en Vinted (consolas, GPUs, smartphones,
cámaras, audio) **para revender en el propio Vinted** (canal principal; Wallapop como alternativa).

> Lee primero **[ARCHITECTURE.md](ARCHITECTURE.md)**: riesgos, viabilidad, estrategia antibot,
> método de valoración y roadmap. La §2.5 explica la decisión de revender en Vinted y sus riesgos
> (cuentas estándar vs. **Vinted Pro**).

## Estado

| Módulo | Estado |
|---|---|
| 1 · Ingesta (API de catálogo de Vinted, resiliencia completa) | ✅ |
| 2 · Normalizador (SKU, estado, accesorios, señales de alarma) | ✅ núcleo |
| 3 · Valoración contra el mercado propio de Vinted + price book | ✅ núcleo (liquidez pendiente de M1.1) |
| 4 · Alertas Telegram / Discord | ✅ |
| 5 · CLI avanzada / dashboard / registro de operaciones | ⏳ |

## Puesta en marcha

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # o: pip install -r requirements-dev.txt
cp .env.example .env              # ajusta costes, umbrales y canales de alerta

pytest                            # tests unitarios (sin red)
pytest -m live                    # test de contrato contra Vinted real (hazlo primero)
vinted-arb init-db                # crea data/vinted_arb.sqlite3
vinted-arb poll --once            # una pasada por las búsquedas de config/searches.yaml
vinted-arb poll                   # bucle continuo: ingesta → valoración → alertas (Ctrl+C)
```

La primera pasada de cada búsqueda es una **línea base**: registra lo existente sin alertar.
Desde la segunda, cada anuncio nuevo o rebajado se valora y, si cumple los umbrales, se alerta.

## Alertas por Telegram

1. Habla con [@BotFather](https://t.me/BotFather), crea un bot (`/newbot`) y copia el token.
2. Escribe cualquier mensaje a tu bot y abre
   `https://api.telegram.org/bot<TOKEN>/getUpdates` para ver tu `chat.id`.
3. En `.env`: `VA_TELEGRAM_BOT_TOKEN=…` y `VA_TELEGRAM_CHAT_ID=…`.
4. `vinted-arb alert-test` envía un mensaje de prueba.

Cada alerta trae la foto, el desglose del margen (compra, tarifa Vinted, envío, reserva de riesgo,
venta esperada con P25/P50/P75 del mercado) y un botón **Abrir en Vinted**. Discord funciona igual
con `VA_DISCORD_WEBHOOK_URL`.

## Valoración: de dónde sale el precio de venta

- **Mercado de Vinted** (automático): anuncios comparables del mismo SKU y estado que el sistema
  va acumulando. Precio esperado = P30 de los comparables × κ (0,92 por defecto).
- **Price book** (manual, opcional): `config/price_book.csv` para arrancar el primer día, antes de
  tener comparables. Formato (valores de ejemplo: pon tus propios precios):

  ```csv
  sku,condition,price_eur,updated_at,notes
  console:ps5-slim:digital,,330,2026-10-09,precio de venta rápida en "Muy bueno"
  phone:iphone-13-pro:128gb,good,420,2026-10-09,
  ```

  `condition` vacío = "Muy bueno" (se ajusta al resto de estados con multiplicadores). Usa los SKUs
  que devuelve `vinted-arb parse-title`. Los precios manuales pierden confianza a los 30 y 90 días.

## Comandos útiles

```bash
vinted-arb market                                   # precio de mercado por SKU (Muy bueno)
vinted-arb market --sku console:ps5-slim:digital --condition good
vinted-arb opps --decision alert -v                 # últimas oportunidades y sus motivos
vinted-arb opps --decision watch                    # casi-oportunidades (análisis modo sombra)
vinted-arb evaluate "PS5 Slim digital 1TB" --price 215 --status "Muy bueno"
vinted-arb fees --buy 220 --sell 300                # desglose (canal por defecto: vinted)
vinted-arb fees --sell 400 --min-net 30 --min-roi 0.20   # precio máximo de compra
vinted-arb parse-title "iPhone 13 Pro 128GB batería 88%" --status "Bueno"
vinted-arb runs                                     # salud de la ingesta
```

## Principios de operación

- **Bajo perfil**: ritmo humano, una sesión coherente, *circuit breaker* que **se detiene** ante
  captcha/403. Sin resolución de captchas, sin rotación de IPs, sin automatizar cuentas.
- **Ingesta anónima**: nunca uses tu cuenta personal de Vinted para la ingesta.
- **Revender en Vinted**: fotos propias siempre; con volumen habitual, cuenta **Vinted Pro**
  (Vinted banea cuentas estándar con actividad comercial).
- **Modo sombra primero**: 2–4 semanas detectando sin comprar para validar KPIs (ARCHITECTURE.md §2.4).
