# 🕵️‍♂️ Deal Hunter Bot

**Deal Hunter Bot** è un bot Telegram progettato per monitorare automaticamente **Vinted** alla ricerca di affari (deal). Calcola dinamicamente il prezzo di mercato degli oggetti monitorati, filtra automaticamente i falsi positivi (come manuali, scatole vuote o pezzi di ricambio) e invia notifiche in tempo reale non appena rileva un annuncio con un prezzo inferiore alla soglia desiderata (di default, il 30% in meno rispetto al valore di mercato).

---

## 🚀 Funzionalità Principali

*   **Monitoraggio automatico:** Ricerca e tracciamento continuo su Vinted.
*   **Stima Dinamica del Prezzo di mercato:** Calcola il valore medio basato sugli annunci attuali e venduti recentemente, escludendo i valori anomali (outliers) per evitare distorsioni.
*   **Filtro Antispam & Falsi Positivi:**
    *   **Keyword Negative Fisse:** Esclusione di termini comuni come *rotto*, *pezzi*, *ricambio*, *only*, ecc.
    *   **Generazione Dinamica di Keyword Negative:** Analizza statisticamente i titoli degli annunci eccessivamente economici (es. custodie o libretti di istruzioni che costano molto meno dell'oggetto reale) e ne estrae automaticamente le parole chiave da filtrare.
*   **Gestione Anti-Bot Avanzata:** Utilizza Playwright in modalità stealth con rotazione dello user-agent e del viewport per bypassare i sistemi di sicurezza come Cloudflare.
*   **Persistenza dei Dati:** Database SQLite per salvare le ricerche attive e tracciare le offerte già notificate per evitare spam e doppioni.
*   **Notifiche Telegram Immediate:** Avvisi formattati in Markdown con dettagli sull'oggetto, prezzo, percentuale di sconto e link diretto.

---

## 🛠️ Architettura del Progetto

Il progetto è strutturato in modo modulare:

*   [`main.py`](main.py): L'entry point dell'applicazione. Carica le variabili dal file `.env` ed avvia il bot nel venv.
*   [`deal_hunter/bot/app.py`](deal_hunter/bot/app.py): Gestore principale dell'interfaccia Telegram. Gestisce i comandi `/cerca`, `/lista`, `/stop` e pianifica l'esecuzione periodica (ogni 10 minuti) dei controlli sulle offerte.
*   [`deal_hunter/vinted/market.py`](deal_hunter/vinted/market.py): Logica per il calcolo del prezzo medio su Vinted. Se le API dirette falliscono, passa al fallback di Playwright.
*   [`deal_hunter/vinted/playwright_scraper.py`](deal_hunter/vinted/playwright_scraper.py): Scraper basato su Playwright per scansionare Vinted simulando un browser reale.
*   [`deal_hunter/bot/notifica.py`](deal_hunter/bot/notifica.py): Comando `/notifica` (configurazione guidata dei filtri) e controllo periodico dei nuovi annunci per le ricerche salvate.
*   [`deal_hunter/vinted/filters.py`](deal_hunter/vinted/filters.py): Categorie, brand e taglie di Vinted, costruzione del link di ricerca filtrato e lettura degli annunci dalla pagina dei risultati.
*   [`deal_hunter/core/utils.py`](deal_hunter/core/utils.py): Contiene costanti di filtraggio (stop words, negative keywords) e funzioni matematiche/statistiche per pulire i prezzi ed estrarre dinamicamente i filtri.
*   [`deal_hunter/core/db.py`](deal_hunter/core/db.py): Script per inizializzare il database SQLite (`deals.db`) ed effettuare eventuali migrazioni di schema.
*   [`deal-hunter.service`](file:///home/danilo/progetti/deal-hunter/deal-hunter.service): Configurazione Systemd per deployare il bot come servizio in background su sistemi Linux.

---

## 📦 Installazione e Setup

### Prerequisiti
*   Python 3.10 o superiore
*   Google Chrome / Chromium (gestito da Playwright)
*   Un token API per un Bot Telegram (ottenibile tramite [@BotFather](https://t.me/BotFather))

### 1. Clonazione e Setup dell'Ambiente Virtuale
Posizionati nella cartella del progetto e crea un ambiente virtuale Python:

```bash
python3 -m venv venv
source venv/bin/activate
```

### 2. Installazione delle Dipendenze
Installa i pacchetti Python richiesti e scarica i browser per Playwright:

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Configurazione Ambientale
Crea un file `.env` partendo da `.env.example` ed inserisci il tuo token Telegram:

```bash
cp .env.example .env
```

Modifica il file `.env` aggiungendo il token:
```env
TELEGRAM_TOKEN=il_tuo_token_telegram_qui
```

### 4. Inizializzazione del Database
Inizializza le tabelle SQLite necessarie al funzionamento del bot:

```bash
python3 -m deal_hunter.core.db
```

---

## 🚦 Esecuzione

### Esecuzione Manuale
Puoi avviare il bot direttamente da terminale:

```bash
python3 main.py
```

### Esecuzione in Background con Systemd (Consigliato per Server)
1. Copia il file di servizio nella directory di systemd:
   ```bash
   sudo cp deal-hunter.service /etc/systemd/system/
   ```
2. Ricarica la configurazione di systemd ed avvia il servizio:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable deal-hunter.service
   sudo systemctl start deal-hunter.service
   ```
3. Per controllare i log in tempo reale:
   ```bash
   journalctl -u deal-hunter.service -f
   ```

---

## 🤖 Comandi del Bot Telegram

Interagisci con il bot nella chat Telegram utilizzando i seguenti comandi:

| Comando | Descrizione | Esempio |
| :--- | :--- | :--- |
| `/start` | Avvia il bot e ricevi il messaggio di benvenuto. | `/start` |
| `/help` | Mostra la guida all'uso del bot e dei comandi. | `/help` |
| `/cerca [oggetto]` | Avvia il monitoraggio con prezzo target automatico (valore di mercato **-30%**). | `/cerca Nintendo 3DS` |
| `/cerca [oggetto] \| [prezzo]` | Avvia il monitoraggio impostando una soglia di prezzo massimo personalizzata. | `/cerca Nintendo 3DS \| 80` |
| `/lista` | Mostra gli oggetti attualmente monitorati con la possibilità di rimuoverli tramite pulsante inline. | `/lista` |
| `/stop [oggetto]` | Interrompe il monitoraggio per l'oggetto specificato. | `/stop Nintendo 3DS` |
| `/notifica` | Configurazione guidata (categoria → brand → taglie): ricevi un avviso per **ogni nuovo annuncio** pubblicato con quei filtri, a qualsiasi prezzo. Accetta anche il **nome esatto di un prodotto** (avviso solo per gli annunci che lo hanno nel titolo) e il link di una ricerca Vinted già filtrata. | `/notifica`, `/notifica jeans skinny uomo`, `/notifica logitech mx vertical` |
| `/notifiche` | Mostra le notifiche nuovi annunci attive, con il link alla ricerca e il pulsante per rimuoverle. | `/notifiche` |
| `/annulla` | Esce dalla configurazione di una notifica. | `/annulla` |

Digitando `/` nella chat Telegram mostra l'elenco di tutti i comandi (il bot lo registra a ogni avvio).

### 🔔 Come Funzionano le Notifiche Nuovi Annunci
1. `/notifica` ti chiede categoria, brand e taglie (ogni passo si può saltare, ma serve almeno categoria o brand) e costruisce il link Vinted con tutti i filtri attivi, ordinato dai più recenti.
2. In alternativa scrivi il nome esatto di un prodotto (es. `rtx 5060`, `logitech mx vertical`) e tocca il pulsante 🔎 **Prodotto**: il bot usa la ricerca testuale di Vinted ma ti segnala solo gli annunci che hanno **tutte le parole nel titolo** (in qualsiasi ordine, anche scritte attaccate come `RTX5060Ti`), scartando i modelli simili che Vinted aggiunge ai risultati.
3. Gli annunci già presenti vengono solo registrati: non ricevi nulla per quelli.
4. Ogni **2 minuti** (`ALERT_INTERVAL_SECONDS`) il bot rilegge la ricerca: per ogni annuncio nuovo ricevi titolo, brand, taglia, prezzo, il link dell'annuncio e il link della ricerca filtrata.
5. Se in un controllo escono più di 8 annunci nuovi (`ALERT_MAX_NOTIFY`) ricevi i più recenti e un riepilogo con il numero degli altri.

### 💡 Come Funziona il Monitoraggio
1. Quando digiti `/cerca Nintendo 3DS`, il bot effettua una ricerca preliminare per calcolare la mediana dei prezzi attuali.
2. Identifica e memorizza filtri dinamici (es. se trova molti annunci "scatola" o "manuale" a 5-10€, aggiunge automaticamente questi termini alle keyword negative).
3. Ogni **10 minuti**, controlla i nuovi annunci pubblicati su Vinted che rispettano il prezzo massimo stabilito e non contengono le keyword negative.
4. Se viene trovato un affare valido, ti invia una notifica immediata. Le offerte già notificate vengono salvate nel DB per non inviarti doppioni.

---

## 🛡️ Licenza
Questo progetto è fornito ad uso personale per l'automazione delle ricerche su mercati online. Si consiglia di fare riferimento ai Termini di Servizio di Vinted per l'utilizzo degli strumenti di scraping.
